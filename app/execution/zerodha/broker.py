"""Zerodha (Kite Connect) execution adapter.

Safety
- `place_order` re-checks the LiveTradingGuard and a hard per-order lot cap: the last line of defence.
- `place_order` is NEVER retried. A definite rejection (input/order/permission/token error) raises
  OrderRejected; anything ambiguous (network error, timeout, 5xx) raises BrokerError so the OMS marks
  the order UNKNOWN and reconciles via the order book using the order `tag` (= client order id).
- cancel/modify/reads are retried with backoff (safe to repeat).

Order updates arrive through two channels and are de-duplicated: WebSocket postbacks
(`on_postback`, from KiteTicker.on_order_update) and periodic order-book polling (`poll`).
"""

from __future__ import annotations

import time
from typing import Callable

import requests
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kex

from app.core.clock import Clock, to_ist
from app.core.models import (AccountBalance, BrokerPosition, Instrument, Order, OrderStatus, OrderType, OrderUpdate,
                             TradingMode)
from app.execution.base import BrokerError, BrokerInterface, OrderRejected
from app.execution.charges import ChargesModel
from app.execution.zerodha.guard import LiveTradingGuard

_DEFINITE = (kex.InputException, kex.OrderException, kex.PermissionException, kex.TokenException)
_AMBIGUOUS = (kex.NetworkException, kex.GeneralException, kex.DataException, requests.RequestException)

_STATUS = {
    "COMPLETE": OrderStatus.FILLED, "REJECTED": OrderStatus.REJECTED, "CANCELLED": OrderStatus.CANCELLED,
    "OPEN": OrderStatus.OPEN, "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
}


def map_status(kite_status: str, filled: int, qty: int) -> OrderStatus:
    st = _STATUS.get(kite_status or "", OrderStatus.SUBMITTED)  # PUT ORDER REQ RECEIVED, VALIDATION PENDING, ...
    if st == OrderStatus.OPEN and 0 < filled < qty:
        return OrderStatus.PARTIALLY_FILLED
    if st == OrderStatus.CANCELLED and filled > 0:
        return OrderStatus.CANCELLED  # partially filled then cancelled; filled qty is kept
    return st


def _retry(fn: Callable, attempts: int = 3, backoff: float = 0.5):
    for i in range(attempts):
        try:
            return fn()
        except _AMBIGUOUS as e:
            if i == attempts - 1:
                raise BrokerError(str(e), retryable=True) from e
            time.sleep(backoff * (2 ** i))
        except _DEFINITE as e:
            raise BrokerError(str(e)) from e


class ZerodhaBroker(BrokerInterface):
    mode = TradingMode.LIVE

    def __init__(self, kite: KiteConnect, guard: LiveTradingGuard, instruments: Callable[[str], Instrument],
                 clock: Clock, max_order_lots: int = 1, charges: ChargesModel | None = None,
                 fallback_margin: Callable[[Order, Instrument, float], float] | None = None):
        super().__init__()
        self.kite = kite
        self.guard = guard
        self.instrument = instruments
        self.clock = clock
        self.max_order_lots = max_order_lots
        self.charges = charges or ChargesModel()
        self._fallback_margin = fallback_margin
        self._orders: dict[str, Order] = {}          # client id -> order
        self._by_broker: dict[str, str] = {}         # broker id -> client id
        self._seen: dict[str, tuple[str, int]] = {}  # client id -> (status, filled) last emitted
        self._turnover: dict[str, float] = {}

    # ---------------------------------------------------------------- orders
    def place_order(self, order: Order) -> str:
        reason = self.guard.block_reason()
        if reason:
            raise OrderRejected(f"live guard: {reason}")
        inst = self.instrument(order.instrument)
        if order.quantity > self.max_order_lots * inst.lot_size:
            raise OrderRejected(f"quantity {order.quantity} exceeds LIVE_MAX_ORDER_LOTS={self.max_order_lots} "
                                f"(lot {inst.lot_size})")
        params = dict(variety=self.kite.VARIETY_REGULAR, exchange=inst.exchange, tradingsymbol=inst.tradingsymbol,
                      transaction_type=str(order.side), quantity=order.quantity, product=str(order.product),
                      order_type=str(order.order_type), validity=self.kite.VALIDITY_DAY,
                      tag=order.client_order_id[:20])
        if order.price is not None and order.order_type in (OrderType.LIMIT, OrderType.SL):
            params["price"] = order.price
        if order.trigger_price is not None and order.order_type in (OrderType.SL, OrderType.SLM):
            params["trigger_price"] = order.trigger_price
        if order.order_type in (OrderType.MARKET, OrderType.SLM):
            params["market_protection"] = self.kite.MARKET_PROTECTION_AUTO
        self._orders[order.client_order_id] = order
        try:
            bid = self.kite.place_order(**params)
        except _DEFINITE as e:
            raise OrderRejected(str(e)) from e
        except _AMBIGUOUS as e:
            raise BrokerError(f"place_order outcome unknown: {e}") from e
        self._by_broker[str(bid)] = order.client_order_id
        return str(bid)

    def modify_order(self, order: Order, *, quantity=None, price=None, trigger_price=None, order_type=None) -> None:
        if not order.broker_order_id:
            raise BrokerError("order has no broker id yet")
        params = {k: v for k, v in dict(quantity=quantity, price=price, trigger_price=trigger_price,
                                        order_type=order_type).items() if v is not None}
        if (order_type or order.order_type) in (OrderType.MARKET, OrderType.SLM, "MARKET", "SL-M"):
            params["market_protection"] = self.kite.MARKET_PROTECTION_AUTO
        _retry(lambda: self.kite.modify_order(self.kite.VARIETY_REGULAR, order.broker_order_id, **params))

    def cancel_order(self, order: Order) -> None:
        if not order.broker_order_id:
            raise BrokerError("order has no broker id yet")
        _retry(lambda: self.kite.cancel_order(self.kite.VARIETY_REGULAR, order.broker_order_id))

    # ---------------------------------------------------------------- order state
    def _client_id(self, row: dict) -> str | None:
        cid = self._by_broker.get(str(row.get("order_id")))
        if cid:
            return cid
        tag = row.get("tag") or ""
        for cid in self._orders:
            if cid[:20] == tag:
                self._by_broker[str(row.get("order_id"))] = cid
                return cid
        return None

    def _to_update(self, row: dict, cid: str) -> OrderUpdate:
        filled = int(row.get("filled_quantity") or 0)
        qty = int(row.get("quantity") or 0)
        ts = row.get("exchange_update_timestamp") or row.get("order_timestamp")
        ts = to_ist(ts) if hasattr(ts, "tzinfo") else self.clock.now()
        return OrderUpdate(cid, map_status(row.get("status"), filled, qty), ts, str(row.get("order_id")), filled,
                           float(row.get("average_price") or 0), reject_reason=row.get("status_message"))

    def _emit_if_new(self, u: OrderUpdate) -> None:
        o = self._orders.get(u.client_order_id)
        prev_status, prev_filled = self._seen.get(u.client_order_id, ("", o.filled_qty if o else 0))
        if (str(u.status), u.filled_qty) == (prev_status, prev_filled) or u.filled_qty < prev_filled:
            return  # duplicate or stale (out-of-order) update
        inc = u.filled_qty - prev_filled
        if inc > 0 and o is not None:
            prev_val = (o.avg_fill_price or 0) * prev_filled
            u.fill_qty = inc
            u.fill_price = (u.avg_fill_price * u.filled_qty - prev_val) / inc if prev_filled else u.avg_fill_price
            inst = self.instrument(o.instrument)
            before = self._turnover.get(u.client_order_id, 0.0)
            u.charges = self.charges.for_fill(inst, o.product, o.side, inc, u.fill_price, before).total  # estimate
            self._turnover[u.client_order_id] = before + inc * u.fill_price
        self._seen[u.client_order_id] = (str(u.status), u.filled_qty)
        self._emit(u)

    def on_postback(self, data: dict) -> None:
        """KiteTicker order update (call from the engine thread)."""
        cid = self._client_id(data)
        if cid:
            self._emit_if_new(self._to_update(data, cid))

    def poll(self) -> None:
        """Order-book poll; catches postbacks missed during WebSocket disconnects."""
        for row in _retry(self.kite.orders) or []:
            cid = self._client_id(row)
            if cid:
                self._emit_if_new(self._to_update(row, cid))

    def get_order_status(self, order: Order) -> OrderUpdate | None:
        if not order.broker_order_id:
            return None
        hist = _retry(lambda: self.kite.order_history(order.broker_order_id))
        return self._to_update(hist[-1], order.client_order_id) if hist else None

    def get_orders(self) -> list[OrderUpdate]:
        out = []
        for row in _retry(self.kite.orders) or []:
            cid = self._client_id(row)
            if cid:
                out.append(self._to_update(row, cid))
        return out

    def raw_orders(self) -> list[dict]:
        return _retry(self.kite.orders) or []

    def get_positions(self) -> list[BrokerPosition]:
        net = (_retry(self.kite.positions) or {}).get("net", [])
        return [BrokerPosition(f"{p['exchange']}:{p['tradingsymbol']}", int(p["quantity"]), float(p["average_price"]),
                               float(p.get("last_price") or 0), float(p.get("pnl") or 0), p.get("product", ""))
                for p in net]

    def get_account_balance(self) -> AccountBalance:
        m = (_retry(lambda: self.kite.margins("equity")) or {})
        avail = m.get("available") or {}
        used = m.get("utilised") or {}
        return AccountBalance(cash=float(avail.get("cash") or 0), available_margin=float(m.get("net") or 0),
                              used_margin=float(used.get("debits") or 0),
                              realized_pnl=float(used.get("m2m_realised") or 0))

    def estimate_margin(self, order: Order, instrument: Instrument, price: float) -> float:
        try:
            res = _retry(lambda: self.kite.order_margins([{
                "exchange": instrument.exchange, "tradingsymbol": instrument.tradingsymbol,
                "transaction_type": str(order.side), "variety": "regular", "product": str(order.product),
                "order_type": "MARKET", "quantity": order.quantity, "price": 0, "trigger_price": 0}]))
            return float(res[0]["total"]) if res else 0.0
        except BrokerError:
            if self._fallback_margin:
                return self._fallback_margin(order, instrument, price)
            raise
