"""Paper broker: a local exchange simulator. NEVER sends anything to Zerodha.

Two price-driving modes share all order logic:
- `on_quote()`  (paper trading) - driven by live ticks; uses bid/ask and top-of-book quantity.
- `on_bar()`    (backtesting)   - driven by OHLC bars with conservative intrabar assumptions:
    * orders placed on a bar close are filled from the *next* bar (no look-ahead);
    * gaps fill at the open (worse for stops, better for limits, as on a real exchange);
    * when a stop and a target could both trigger inside one bar, the stop is processed first.

Realism knobs: slippage (bps) on market/stop-market fills, partial fills limited by top-of-book
quantity or a share of bar volume, lot-size/tick-size validation, margin checks, full charges.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from app.core.clock import Clock
from app.core.models import (AccountBalance, BrokerPosition, Candle, Instrument, Order, OrderStatus, OrderType,
                             OrderUpdate, Product, Side, TradingMode)
from app.execution.base import BrokerInterface, BrokerError
from app.execution.charges import ChargesModel


@dataclass
class PaperConfig:
    capital: float = 500_000.0
    slippage_bps: float = 2.0                  # adverse slippage on MARKET / SL-M fills
    partial_fills: bool = True
    max_volume_participation: float = 0.25     # bar mode: max share of a bar's volume per order per bar
    limit_requires_trade_through: bool = False # bar mode: require low < limit (not just touch) for buys
    futures_margin_pct: float = 0.15           # rough SPAN+exposure approximation for futures
    option_short_margin_pct: float = 0.09      # of strike notional, for short options (live: Kite margin API)
    equity_intraday_margin_pct: float = 0.20   # MIS (5x)
    check_margin: bool = True


@dataclass
class _Quote:
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    ts: datetime | None = None


@dataclass
class _Pos:
    qty: int = 0
    avg: float = 0.0
    realized: float = 0.0
    product: str = ""


@dataclass
class _Working:
    order: Order
    triggered: bool = False
    turnover: float = 0.0
    seq: int = 0


_STOP_TYPES = (OrderType.SL, OrderType.SLM)


class PaperBroker(BrokerInterface):
    mode = TradingMode.PAPER

    def __init__(self, instruments: Callable[[str], Instrument], config: PaperConfig | None = None,
                 charges: ChargesModel | None = None, clock: Clock | None = None):
        super().__init__()
        self.cfg = config or PaperConfig()
        self.charges = charges or ChargesModel()
        self.clock = clock or Clock()
        self._instrument = instruments
        # Optional exact margin source (e.g. Kite order_margins when logged in); returns None to fall back.
        self.margin_override: Callable[[Order, Instrument, float], float | None] | None = None
        self.orders: dict[str, _Working] = {}
        self.positions: dict[str, _Pos] = {}
        self.quotes: dict[str, _Quote] = {}
        self.cash = self.cfg.capital
        self.total_charges = 0.0
        self._seq = 0
        self._ids = 0

    # ------------------------------------------------------------------ helpers
    def _now(self) -> datetime:
        return self.clock.now()

    def _update(self, w: _Working, status: OrderStatus, fill_qty: int = 0, fill_price: float = 0.0,
                charges: float = 0.0, reason: str | None = None) -> None:
        o = w.order
        o.status = status
        o.updated_at = self._now()
        if reason:
            o.reject_reason = reason
        self._emit(OrderUpdate(o.client_order_id, status, o.updated_at, o.broker_order_id, o.filled_qty,
                               o.avg_fill_price, fill_qty, fill_price, charges, reason))

    def _ref_price(self, instrument: str, fallback: float | None) -> float | None:
        q = self.quotes.get(instrument)
        return q.ltp if q else fallback

    def margin_for(self, inst: Instrument, product: str, qty: int, price: float) -> float:
        if qty <= 0:
            return 0.0
        if inst.is_future:
            return price * qty * self.cfg.futures_margin_pct
        if inst.is_option:
            return price * qty  # long premium; short handled in estimate_margin
        if product == Product.CNC:
            return price * qty
        return price * qty * self.cfg.equity_intraday_margin_pct

    def estimate_margin(self, order: Order, instrument: Instrument, price: float) -> float:
        if self.margin_override is not None:
            try:
                m = self.margin_override(order, instrument, price)
                if m is not None:
                    return m
            except Exception:
                pass
        pos = self.positions.get(order.instrument, _Pos())
        signed = order.quantity * order.side.sign
        # Only the part of the order that increases exposure needs margin.
        if pos.qty == 0 or (pos.qty > 0) == (signed > 0):
            increase = abs(signed)
        else:
            increase = max(0, abs(signed) - abs(pos.qty))
        if instrument.is_option and order.side is Side.SELL and (pos.qty <= 0):
            ref = max(price, instrument.strike or price)
            return ref * increase * self.cfg.option_short_margin_pct
        return self.margin_for(instrument, order.product, increase, price)

    def used_margin(self) -> float:
        total = 0.0
        for key, p in self.positions.items():
            if p.qty == 0:
                continue
            inst = self._instrument(key)
            if inst.is_option and p.qty < 0:
                total += max(p.avg, inst.strike or p.avg) * abs(p.qty) * self.cfg.option_short_margin_pct
            else:
                total += self.margin_for(inst, p.product, abs(p.qty), p.avg)
        return total

    def unrealized(self) -> float:
        u = 0.0
        for key, p in self.positions.items():
            if p.qty:
                u += (self._ref_price(key, p.avg) - p.avg) * p.qty
        return u

    def realized(self) -> float:
        return sum(p.realized for p in self.positions.values())

    # ------------------------------------------------------------------ BrokerInterface
    def place_order(self, order: Order) -> str:
        self._ids += 1
        order.broker_order_id = f"PAPER{self._ids:08d}"
        order.created_at = order.created_at or self._now()
        self._seq += 1
        w = _Working(order, seq=self._seq)
        self.orders[order.client_order_id] = w
        try:
            inst = self._instrument(order.instrument)
        except KeyError:
            self._update(w, OrderStatus.REJECTED, reason=f"Unknown instrument {order.instrument}")
            return order.broker_order_id
        reason = self._validate(order, inst)
        if reason:
            self._update(w, OrderStatus.REJECTED, reason=reason)
            return order.broker_order_id
        status = OrderStatus.TRIGGER_PENDING if order.order_type in _STOP_TYPES else OrderStatus.OPEN
        self._update(w, status)
        return order.broker_order_id

    def _validate(self, o: Order, inst: Instrument) -> str | None:
        if not inst.tradable:
            return f"{inst.key} is not tradable (index)"
        if o.quantity <= 0 or o.quantity % inst.lot_size:
            return f"Quantity {o.quantity} must be a positive multiple of lot size {inst.lot_size}"
        if o.order_type in (OrderType.LIMIT, OrderType.SL) and not o.price:
            return "Price required for LIMIT/SL orders"
        if o.order_type in _STOP_TYPES and not o.trigger_price:
            return "Trigger price required for SL/SL-M orders"
        for label, px in (("price", o.price), ("trigger", o.trigger_price)):
            if px and abs(round(px / inst.tick_size) * inst.tick_size - px) > 1e-6:
                return f"{label} {px} is not a multiple of tick size {inst.tick_size}"
        if o.order_type in _STOP_TYPES and o.trigger_price:
            ref = self._ref_price(o.instrument, None)
            if ref is not None and ((o.side is Side.BUY and o.trigger_price <= ref) or
                                    (o.side is Side.SELL and o.trigger_price >= ref)):
                return f"Trigger {o.trigger_price} already crossed (LTP {ref})"
        if self.cfg.check_margin:
            px = o.price or self._ref_price(o.instrument, None) or o.trigger_price or 0.0
            need = self.estimate_margin(o, inst, px)
            for other in self.orders.values():   # margin blocked by other working exposure-increasing orders
                oo = other.order
                if oo is not o and oo.status.is_working and oo.instrument != o.instrument:
                    need += self.estimate_margin(oo, self._instrument(oo.instrument),
                                                 oo.price or self._ref_price(oo.instrument, 0) or 0) * \
                        (oo.remaining_qty / oo.quantity)
            avail = self.cash + self.unrealized() - self.used_margin()
            if need > avail + 1e-6:
                return f"Insufficient margin: need {need:,.0f}, available {avail:,.0f}"
        return None

    def modify_order(self, order: Order, *, quantity=None, price=None, trigger_price=None, order_type=None) -> None:
        w = self.orders.get(order.client_order_id)
        if w is None or w.order.status.is_terminal:
            raise BrokerError(f"Order {order.client_order_id} is not open")
        o = w.order
        if quantity is not None:
            if quantity < o.filled_qty:
                raise BrokerError("New quantity below filled quantity")
            o.quantity = quantity
        if price is not None:
            o.price = price
        if trigger_price is not None:
            o.trigger_price = trigger_price
        if order_type is not None:
            o.order_type = OrderType(order_type)
        if o.remaining_qty == 0:
            self._update(w, OrderStatus.FILLED)
        else:
            self._update(w, o.status)

    def cancel_order(self, order: Order) -> None:
        w = self.orders.get(order.client_order_id)
        if w is None or w.order.status.is_terminal:
            return
        self._update(w, OrderStatus.CANCELLED)

    def get_order_status(self, order: Order) -> OrderUpdate | None:
        w = self.orders.get(order.client_order_id)
        if not w:
            return None
        o = w.order
        return OrderUpdate(o.client_order_id, o.status, o.updated_at or self._now(), o.broker_order_id,
                           o.filled_qty, o.avg_fill_price)

    def get_orders(self) -> list[OrderUpdate]:
        return [self.get_order_status(w.order) for w in self.orders.values()]

    def get_positions(self) -> list[BrokerPosition]:
        out = []
        for k, p in self.positions.items():
            if p.qty or p.realized:
                ltp = self._ref_price(k, p.avg)
                out.append(BrokerPosition(k, p.qty, p.avg, ltp, p.realized + (ltp - p.avg) * p.qty, p.product))
        return out

    def get_account_balance(self) -> AccountBalance:
        used = self.used_margin()
        return AccountBalance(cash=round(self.cash, 2), available_margin=round(self.cash + self.unrealized() - used, 2),
                              used_margin=round(used, 2), realized_pnl=round(self.realized(), 2),
                              charges=round(self.total_charges, 2))

    # ------------------------------------------------------------------ fills
    def _fill(self, w: _Working, qty: int, price: float) -> None:
        o = w.order
        inst = self._instrument(o.instrument)
        qty = min(qty, o.remaining_qty)
        if qty <= 0:
            return
        price = inst.round_to_tick(price)
        ch = self.charges.for_fill(inst, o.product, o.side, qty, price, w.turnover).total
        w.turnover += qty * price
        prev_filled = o.filled_qty
        o.filled_qty += qty
        o.avg_fill_price = (o.avg_fill_price * prev_filled + price * qty) / o.filled_qty
        self.cash -= ch
        self.total_charges += ch
        self._apply_position(o.instrument, o.side.sign * qty, price, o.product)
        status = OrderStatus.FILLED if o.remaining_qty == 0 else OrderStatus.PARTIALLY_FILLED
        self._update(w, status, qty, price, ch)

    def _apply_position(self, key: str, signed_qty: int, price: float, product: str) -> None:
        p = self.positions.setdefault(key, _Pos(product=product))
        if p.qty == 0 or (p.qty > 0) == (signed_qty > 0):
            new = p.qty + signed_qty
            p.avg = (p.avg * abs(p.qty) + price * abs(signed_qty)) / abs(new)
            p.qty = new
            return
        closing = min(abs(signed_qty), abs(p.qty))
        pnl = (price - p.avg) * closing * (1 if p.qty > 0 else -1)
        p.realized += pnl
        self.cash += pnl
        flips = abs(signed_qty) > abs(p.qty)
        p.qty += signed_qty
        if flips:
            p.avg = price
        elif p.qty == 0:
            p.avg = 0.0

    def _lots(self, inst: Instrument, qty: float, remaining: int) -> int:
        lot = inst.lot_size
        q = int(qty // lot) * lot
        return min(remaining, max(lot, q))

    def _slip(self, price: float, side: Side) -> float:
        return price * (1 + side.sign * self.cfg.slippage_bps / 1e4)

    def _working_for(self, instrument: str) -> list[_Working]:
        ws = [w for w in self.orders.values() if w.order.instrument == instrument and w.order.status.is_working]
        # Stops first (pessimistic), then market, then limits; FIFO within each group.
        rank = {OrderType.SLM: 0, OrderType.SL: 0, OrderType.MARKET: 1, OrderType.LIMIT: 2}
        return sorted(ws, key=lambda w: (rank[w.order.order_type], w.seq))

    # ---- tick mode
    def on_quote(self, instrument: str, ltp: float, bid: float | None = None, ask: float | None = None,
                 bid_qty: int | None = None, ask_qty: int | None = None, ts: datetime | None = None) -> None:
        self.quotes[instrument] = _Quote(ltp, bid, ask, bid_qty, ask_qty, ts)
        inst = None
        for w in self._working_for(instrument):
            o = w.order
            if not o.status.is_working:  # may have been cancelled by an OCO sibling during this loop
                continue
            inst = inst or self._instrument(instrument)
            buy = o.side is Side.BUY
            touch = (ask if buy else bid) or ltp
            touch_qty = (ask_qty if buy else bid_qty) if self.cfg.partial_fills else None
            if o.order_type in _STOP_TYPES and not w.triggered:
                if (buy and ltp >= o.trigger_price) or (not buy and ltp <= o.trigger_price):
                    w.triggered = True
                    self._update(w, OrderStatus.OPEN)
                else:
                    continue
            if o.order_type in (OrderType.MARKET, OrderType.SLM):
                px = self._slip(touch, o.side)
            else:  # LIMIT or triggered SL
                if (buy and touch > o.price) or (not buy and touch < o.price):
                    continue
                px = min(o.price, touch) if buy else max(o.price, touch)
            qty = self._lots(inst, touch_qty, o.remaining_qty) if touch_qty else o.remaining_qty
            self._fill(w, qty, px)

    # ---- bar mode
    def on_bar(self, instrument: str, bar: Candle) -> None:
        """Fill working orders against one OHLC bar. Orders created while processing this bar (e.g.
        protective orders after an entry fill) are evaluated in later passes over the same bar."""
        inst = self._instrument(instrument)
        budget: dict[str, float] = {}
        ref = bar.open  # "current price" within the bar: the open, then the latest fill price
        self.quotes[instrument] = _Quote(ref, ts=bar.ts)
        for _ in range(6):
            progressed = False
            for w in self._working_for(instrument):
                o = w.order
                if not o.status.is_working:
                    continue
                px = self._bar_fill_price(w, bar, ref)
                if px is None:
                    continue
                cap = o.remaining_qty
                if self.cfg.partial_fills and bar.volume > 0:
                    left = budget.setdefault(o.client_order_id, bar.volume * self.cfg.max_volume_participation)
                    if left < inst.lot_size and o.filled_qty > 0:
                        continue
                    cap = self._lots(inst, left, o.remaining_qty)
                    budget[o.client_order_id] = max(0.0, left - cap)
                before = o.filled_qty
                ref = px
                self.quotes[instrument] = _Quote(px, ts=bar.ts)
                self._fill(w, cap, px)
                progressed |= o.filled_qty > before
            if not progressed:
                break
        self.quotes[instrument] = _Quote(bar.close, ts=bar.ts)

    def _bar_fill_price(self, w: _Working, bar: Candle, ref_open: float) -> float | None:
        o = w.order
        buy = o.side is Side.BUY
        if o.order_type in _STOP_TYPES and not w.triggered:
            t = o.trigger_price
            if buy and bar.high >= t:
                start = max(ref_open, t)
            elif not buy and bar.low <= t:
                start = min(ref_open, t)
            else:
                return None
            w.triggered = True
            if o.order_type is OrderType.SLM:
                return self._slip(start, o.side)
            # stop-limit: fill at the trigger/gap price if within the limit, else rest as a limit
            if (buy and start <= o.price) or (not buy and start >= o.price):
                return start
            return None
        if o.order_type is OrderType.MARKET:
            return self._slip(ref_open, o.side)
        # LIMIT (or triggered SL)
        p = o.price
        if buy:
            if ref_open <= p:
                return ref_open
            hit = bar.low < p if self.cfg.limit_requires_trade_through else bar.low <= p
            return p if hit else None
        if ref_open >= p:
            return ref_open
        hit = bar.high > p if self.cfg.limit_requires_trade_through else bar.high >= p
        return p if hit else None

    # ------------------------------------------------------------------ session / persistence
    def expire_day_orders(self, cutoff: datetime | None = None) -> None:
        """Cancel DAY orders. Orders created at/after `cutoff` (the session close) are treated as
        after-market orders for the next session and survive."""
        for w in list(self.orders.values()):
            o = w.order
            if o.status.is_working and (cutoff is None or o.created_at is None or o.created_at < cutoff):
                self._update(w, OrderStatus.CANCELLED, reason="Expired at end of day")

    def to_state(self) -> dict:
        return {
            "cash": self.cash, "total_charges": self.total_charges, "ids": self._ids,
            "positions": {k: vars(p) for k, p in self.positions.items()},
            "orders": [{**w.order.as_dict(), "_triggered": w.triggered, "_turnover": w.turnover}
                       for w in self.orders.values() if w.order.status.is_working],
        }

    def load_state(self, state: dict) -> None:
        from app.core.models import OrderRole
        self.cash = state["cash"]
        self.total_charges = state.get("total_charges", 0.0)
        self._ids = state.get("ids", 0)
        self.positions = {k: _Pos(**v) for k, v in state.get("positions", {}).items()}
        for d in state.get("orders", []):
            trig, turn = d.pop("_triggered", False), d.pop("_turnover", 0.0)
            d["side"], d["order_type"] = Side(d["side"]), OrderType(d["order_type"])
            d["product"], d["role"], d["status"] = Product(d["product"]), OrderRole(d["role"]), OrderStatus(d["status"])
            o = Order(**d)
            self._seq += 1
            self.orders[o.client_order_id] = _Working(o, trig, turn, self._seq)
