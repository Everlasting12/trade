"""Order Management System (OMS). The only component that talks to a broker.

Responsibilities
- Entry orders from approved signals, with idempotency keys (duplicate-signal prevention) and a
  one-working-entry-per-instrument rule.
- Bracket management: once an entry (partially) fills, a protective stop (SL-M) and an optional target
  (LIMIT) are placed for the filled quantity and kept in sync. They behave as OCO: when one fills, the
  other is cancelled/reduced. If a protective order is rejected, the position is exited at market:
  the OMS never leaves an unprotected position by design.
- Exits, cancellations, stop modification (trailing), flatten-all for the kill switch.
- Ambiguous broker failures (timeout on place) mark the order UNKNOWN and are reconciled from the
  broker's order book; they are never blindly re-sent (that is how duplicate live orders happen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.core.clock import Clock
from app.core.models import (Fill, Instrument, Order, OrderRole, OrderStatus, OrderType, OrderUpdate, Product, Side,
                             Signal)
from app.execution.base import BrokerError, BrokerInterface, OrderRejected
from app.portfolio.portfolio import Portfolio
from app.utils.events import EventLog


@dataclass
class Bracket:
    entry_id: str
    instrument: str
    strategy: str
    entry_side: Side
    product: Product
    stop_loss: float | None
    target: float | None
    signal_id: str | None
    sl_id: str | None = None
    target_id: str | None = None
    active: bool = True
    meta: dict = field(default_factory=dict)   # option plan / underlying levels / score, for exits + journal


class OrderManager:
    def __init__(self, broker: BrokerInterface, portfolio: Portfolio, instruments: Callable[[str], Instrument],
                 events: EventLog, clock: Clock, persist: Callable[[Order], None] | None = None,
                 on_trade: Callable | None = None, on_fill: Callable[[Fill], None] | None = None,
                 on_open: Callable | None = None, option_stop_buffer_pct: float = 5.0):
        self.broker = broker
        self.portfolio = portfolio
        self.instrument = instruments
        self.events = events
        self.clock = clock
        self.persist = persist or (lambda o: None)
        self.on_trade = on_trade or (lambda t, b=None: None)
        self.on_fill_cb = on_fill or (lambda f: None)
        self.on_open = on_open or (lambda pos, bracket: None)
        self.option_stop_buffer_pct = option_stop_buffer_pct
        self.orders: dict[str, Order] = {}
        self.brackets: dict[str, Bracket] = {}           # entry order id -> bracket
        self._child_to_entry: dict[str, str] = {}
        self._idempotency: dict[str, str] = {}           # key -> client_order_id
        self._exit_reason: dict[str, str] = {}
        broker.set_update_handler(self.on_update)

    # ================================================================ queries
    def working(self, instrument: str | None = None, role: OrderRole | None = None) -> list[Order]:
        return [o for o in self.orders.values() if o.status.is_working or o.status == OrderStatus.CREATED
                if (instrument is None or o.instrument == instrument) and (role is None or o.role == role)]

    def pending_entry_instruments(self) -> frozenset[str]:
        return frozenset(o.instrument for o in self.working(role=OrderRole.ENTRY))

    def bracket_for(self, instrument: str) -> Bracket | None:
        for b in self.brackets.values():
            if b.active and b.instrument == instrument:
                return b
        return None

    # ================================================================ commands
    def submit_entry(self, signal: Signal, quantity: int, product: Product) -> Order | None:
        key = f"{signal.strategy}|{signal.instrument}|{signal.ts.isoformat() if signal.ts else signal.signal_id}|{signal.action}"
        if key in self._idempotency:
            self.events.emit("duplicate_signal_ignored", "WARNING", strategy=signal.strategy,
                             instrument=signal.instrument, order_id=self._idempotency[key], reason=key)
            return None
        if signal.instrument in self.pending_entry_instruments():
            self.events.emit("entry_skipped", "WARNING", strategy=signal.strategy, instrument=signal.instrument,
                             reason="entry order already working")
            return None
        inst = self.instrument(signal.instrument)
        side = signal.side
        otype = signal.entry_order_type
        price = inst.round_to_tick(signal.entry_price) if otype == OrderType.LIMIT and signal.entry_price else None
        order = Order(signal.instrument, side, quantity, otype, product, OrderRole.ENTRY, signal.strategy,
                      price=price, signal_id=signal.signal_id)
        sl = inst.round_to_tick(signal.stop_loss) if signal.stop_loss is not None else None
        tgt = inst.round_to_tick(signal.target) if signal.target is not None else None
        meta = {**(signal.metadata or {}), "score": signal.score, "confidence": signal.confidence,
                "reasons": list(signal.reason), "planned_rr": (signal.metadata or {}).get("planned_rr") or signal.risk_reward,
                "entry_ref": signal.entry_price, "signal_ts": signal.ts.isoformat() if signal.ts else None,
                "timeframe": signal.timeframe}
        self.brackets[order.client_order_id] = Bracket(order.client_order_id, signal.instrument, signal.strategy,
                                                       side, product, sl, tgt, signal.signal_id, meta=meta)
        self._idempotency[key] = order.client_order_id
        self._send(order)
        return order

    def exit_position(self, instrument: str, reason: str) -> Order | None:
        pos = self.portfolio.positions.get(instrument)
        for o in self.working(instrument, OrderRole.ENTRY):
            self.cancel(o, "exit requested")
        b = self.bracket_for(instrument)
        if pos is None:
            if b:
                b.active = False
            return None
        existing = self.working(instrument, OrderRole.EXIT)
        if existing:
            return existing[0]
        if b:
            self._cancel_children(b, "position exit")
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        product = b.product if b else Product.MIS
        order = Order(instrument, side, abs(pos.quantity), OrderType.MARKET, product, OrderRole.EXIT, pos.strategy,
                      parent_id=b.entry_id if b else None, signal_id=pos.signal_id)
        if b:
            self._child_to_entry[order.client_order_id] = b.entry_id
        self._exit_reason[order.client_order_id] = reason
        self.events.emit("exit_requested", strategy=pos.strategy, instrument=instrument, reason=reason,
                         position_id=pos.position_id)
        self._send(order)
        return order

    def modify_stop(self, instrument: str, new_stop: float) -> bool:
        b = self.bracket_for(instrument)
        if not b or not b.sl_id:
            return False
        o = self.orders[b.sl_id]
        if not o.status.is_working:
            return False
        inst = self.instrument(instrument)
        trig = inst.round_to_tick(new_stop)
        limit = None
        if o.order_type == OrderType.SL:
            buf = self.option_stop_buffer_pct / 100
            limit = max(inst.round_to_tick(trig * (1 - buf) if o.side is Side.SELL else trig * (1 + buf)), inst.tick_size)
        try:
            self.broker.modify_order(o, trigger_price=trig, price=limit)
        except BrokerError as e:
            self.events.emit("modify_failed", "ERROR", instrument=instrument, order_id=o.client_order_id, error=str(e))
            return False
        o.trigger_price = trig
        if limit is not None:
            o.price = limit
        b.stop_loss = trig
        self.portfolio.set_bracket(instrument, trig, b.target)
        self.persist(o)
        self.events.emit("stop_modified", strategy=o.strategy, instrument=instrument, order_id=o.client_order_id,
                         stop_loss=trig)
        return True

    def cancel(self, order: Order, reason: str = "") -> None:
        if order.status.is_terminal:
            return
        try:
            self.broker.cancel_order(order)
            self.events.emit("order_cancel_requested", strategy=order.strategy, instrument=order.instrument,
                             order_id=order.client_order_id, reason=reason)
        except BrokerError as e:
            self.events.emit("cancel_failed", "ERROR", instrument=order.instrument, order_id=order.client_order_id,
                             error=str(e))

    def cancel_all(self, reason: str) -> None:
        for o in list(self.working()):
            self.cancel(o, reason)

    def flatten_all(self, reason: str) -> None:
        for o in list(self.working(role=OrderRole.ENTRY)):
            self.cancel(o, reason)
        for instrument in list(self.portfolio.positions):
            self.exit_position(instrument, reason)

    # ================================================================ broker I/O
    def _send(self, order: Order) -> None:
        now = self.clock.now()
        order.created_at = order.updated_at = now
        order.status = OrderStatus.SUBMITTED
        self.orders[order.client_order_id] = order
        self.persist(order)
        self.events.emit("order_submitted", strategy=order.strategy, instrument=order.instrument,
                         action=f"{order.side} {order.quantity} {order.order_type} ({order.role})",
                         order_id=order.client_order_id, price=order.price, trigger=order.trigger_price)
        try:
            bid = self.broker.place_order(order)
            order.broker_order_id = order.broker_order_id or bid
        except OrderRejected as e:
            self.on_update(OrderUpdate(order.client_order_id, OrderStatus.REJECTED, now, reject_reason=str(e)))
        except BrokerError as e:
            order.status = OrderStatus.UNKNOWN
            order.reject_reason = f"send outcome unknown: {e}"
            self.events.emit("order_state_unknown", "ERROR", strategy=order.strategy, instrument=order.instrument,
                             order_id=order.client_order_id, error=str(e))
        self.persist(order)

    def reconcile(self) -> list[str]:
        """Refresh UNKNOWN/working orders from the broker's order book. Returns ids that changed."""
        changed = []
        try:
            book = {u.client_order_id: u for u in self.broker.get_orders()}
        except BrokerError as e:
            self.events.emit("reconcile_failed", "ERROR", error=str(e))
            return changed
        for o in list(self.working()):
            u = book.get(o.client_order_id)
            if u is None:
                if o.status == OrderStatus.UNKNOWN:
                    # Never reached the broker: safe to mark rejected (not re-sent automatically).
                    self.on_update(OrderUpdate(o.client_order_id, OrderStatus.REJECTED, self.clock.now(),
                                               reject_reason="not found at broker after send failure"))
                    changed.append(o.client_order_id)
                continue
            if u.status != o.status or u.filled_qty != o.filled_qty:
                if u.filled_qty > o.filled_qty:
                    # Synthesize the missed fill increment at the broker's average price.
                    prev_val = o.avg_fill_price * o.filled_qty
                    inc = u.filled_qty - o.filled_qty
                    px = (u.avg_fill_price * u.filled_qty - prev_val) / inc if inc else u.avg_fill_price
                    u.fill_qty, u.fill_price = inc, px
                self.on_update(u)
                changed.append(o.client_order_id)
        return changed

    # ================================================================ updates
    def on_update(self, u: OrderUpdate) -> None:
        o = self.orders.get(u.client_order_id)
        if o is None:
            self.events.emit("unknown_order_update", "WARNING", order_id=u.client_order_id, status=str(u.status))
            return
        if o.status.is_terminal and u.status != o.status:
            return  # late/duplicate update after terminal state
        prev_status = o.status
        if u.broker_order_id:
            o.broker_order_id = u.broker_order_id
        if u.fill_qty > 0:
            o.filled_qty = u.filled_qty or (o.filled_qty + u.fill_qty)
            o.avg_fill_price = u.avg_fill_price or u.fill_price
        o.status = u.status
        o.updated_at = u.ts
        if u.reject_reason:
            o.reject_reason = u.reject_reason
        self.persist(o)

        if u.fill_qty > 0:
            self._on_fill(o, u)
        if u.status != prev_status or u.fill_qty:
            level = "WARNING" if u.status == OrderStatus.REJECTED else "INFO"
            name = {OrderStatus.FILLED: "order_filled", OrderStatus.PARTIALLY_FILLED: "order_partially_filled",
                    OrderStatus.REJECTED: "order_rejected", OrderStatus.CANCELLED: "order_cancelled"}.get(
                u.status, "order_status")
            self.events.emit(name, level, strategy=o.strategy, instrument=o.instrument, order_id=o.client_order_id,
                             reason=u.reject_reason, status=str(u.status), filled=o.filled_qty,
                             avg_price=round(o.avg_fill_price, 2), role=str(o.role))

        if u.status == OrderStatus.REJECTED:
            self._on_reject(o)
        elif u.status == OrderStatus.CANCELLED and o.role == OrderRole.ENTRY:
            b = self.brackets.get(o.client_order_id)
            if b and o.filled_qty == 0:
                b.active = False
            elif b:
                self._ensure_protection(b)

    def _on_fill(self, o: Order, u: OrderUpdate) -> None:
        entry_id = o.client_order_id if o.role == OrderRole.ENTRY else (o.parent_id or self._child_to_entry.get(o.client_order_id))
        b = self.brackets.get(entry_id) if entry_id else None
        fill = Fill(o.client_order_id, o.instrument, o.side, u.fill_qty, u.fill_price, u.ts, u.charges, o.strategy, o.role)
        reason = {OrderRole.STOP_LOSS: "stop_loss", OrderRole.TARGET: "target"}.get(o.role) or \
            self._exit_reason.get(o.client_order_id, "")
        self.on_fill_cb(fill)
        had_position = o.instrument in self.portfolio.positions
        trades = self.portfolio.on_fill(fill, b.stop_loss if b and o.role == OrderRole.ENTRY else None,
                                        b.target if b and o.role == OrderRole.ENTRY else None,
                                        o.signal_id, reason)
        pos = self.portfolio.positions.get(o.instrument)
        if not had_position and pos is not None:
            self.on_open(pos, b)
            self.events.emit("position_opened", strategy=o.strategy, instrument=o.instrument,
                             position_id=pos.position_id, order_id=o.client_order_id, quantity=pos.quantity,
                             price=fill.price)
        for t in trades:
            self.events.emit("position_closed", strategy=t.strategy, instrument=t.instrument,
                             position_id=t.position_id, order_id=o.client_order_id, reason=t.exit_reason,
                             net_pnl=round(t.net_pnl, 2), quantity=t.quantity, exit_price=t.exit_price)
            self.on_trade(t, b)
        if b is None:
            return
        if o.role == OrderRole.ENTRY:
            self._ensure_protection(b)
        elif o.role in (OrderRole.STOP_LOSS, OrderRole.TARGET, OrderRole.EXIT):
            if pos is None:
                self._cancel_children(b, f"{o.role} filled")
                b.active = False
            else:
                self._ensure_protection(b)

    def _on_reject(self, o: Order) -> None:
        if o.role == OrderRole.ENTRY:
            b = self.brackets.get(o.client_order_id)
            if b:
                b.active = o.filled_qty > 0
        elif o.role in (OrderRole.STOP_LOSS, OrderRole.TARGET):
            pos = self.portfolio.positions.get(o.instrument)
            if pos is not None and o.role == OrderRole.STOP_LOSS:
                self.events.emit("protective_order_rejected", "ERROR", strategy=o.strategy, instrument=o.instrument,
                                 order_id=o.client_order_id, reason=o.reject_reason)
                self.exit_position(o.instrument, f"stop-loss order rejected: {o.reject_reason}")
        elif o.role == OrderRole.EXIT:
            self.events.emit("exit_rejected", "CRITICAL", strategy=o.strategy, instrument=o.instrument,
                             order_id=o.client_order_id, reason=o.reject_reason)

    # ================================================================ bracket helpers
    def _ensure_protection(self, b: Bracket) -> None:
        """Make SL/target quantities equal the open position; place them if missing."""
        if not b.active:
            return
        pos = self.portfolio.positions.get(b.instrument)
        qty = abs(pos.quantity) if pos else 0
        exit_side = b.entry_side.opposite
        if self.working(b.instrument, OrderRole.EXIT):
            return
        for role, attr, price in ((OrderRole.STOP_LOSS, "sl_id", b.stop_loss), (OrderRole.TARGET, "target_id", b.target)):
            if price is None:
                continue
            cid = getattr(b, attr)
            child = self.orders.get(cid) if cid else None
            if child is not None and child.status.is_working:
                if qty == 0:
                    self.cancel(child, "position closed")
                elif child.remaining_qty != qty:
                    try:
                        self.broker.modify_order(child, quantity=child.filled_qty + qty)
                        child.quantity = child.filled_qty + qty
                        self.persist(child)
                    except BrokerError as e:
                        self.events.emit("modify_failed", "ERROR", instrument=b.instrument,
                                         order_id=child.client_order_id, error=str(e))
                continue
            if qty == 0:
                continue
            if role == OrderRole.STOP_LOSS:
                inst = self.instrument(b.instrument)
                if inst.is_option:
                    # Exchanges don't accept SL-M for options: use a stop-limit with a buffer beyond the trigger.
                    buf = self.option_stop_buffer_pct / 100
                    limit = price * (1 - buf) if exit_side is Side.SELL else price * (1 + buf)
                    order = Order(b.instrument, exit_side, qty, OrderType.SL, b.product, role, b.strategy,
                                  price=max(inst.round_to_tick(limit), inst.tick_size), trigger_price=price,
                                  parent_id=b.entry_id, signal_id=b.signal_id)
                else:
                    order = Order(b.instrument, exit_side, qty, OrderType.SLM, b.product, role, b.strategy,
                                  trigger_price=price, parent_id=b.entry_id, signal_id=b.signal_id)
            else:
                order = Order(b.instrument, exit_side, qty, OrderType.LIMIT, b.product, role, b.strategy,
                              price=price, parent_id=b.entry_id, signal_id=b.signal_id)
            setattr(b, attr, order.client_order_id)
            self._child_to_entry[order.client_order_id] = b.entry_id
            self._send(order)

    def _cancel_children(self, b: Bracket, reason: str) -> None:
        for cid in (b.sl_id, b.target_id):
            if cid and cid in self.orders:
                self.cancel(self.orders[cid], reason)

    # ================================================================ persistence
    def to_state(self) -> dict:
        return {
            "brackets": [vars(b) | {"entry_side": str(b.entry_side), "product": str(b.product)}
                         for b in self.brackets.values() if b.active],
            "idempotency": self._idempotency,
            "orders": [o.as_dict() for o in self.orders.values() if not o.status.is_terminal],
        }

    def load_state(self, state: dict) -> None:
        from app.core.models import OrderRole as R
        for d in state.get("orders", []):
            d = dict(d)
            d["side"], d["order_type"], d["product"] = Side(d["side"]), OrderType(d["order_type"]), Product(d["product"])
            d["role"], d["status"] = R(d["role"]), OrderStatus(d["status"])
            o = Order(**d)
            self.orders[o.client_order_id] = o
            if o.parent_id:
                self._child_to_entry[o.client_order_id] = o.parent_id
        for d in state.get("brackets", []):
            d = dict(d)
            d["entry_side"], d["product"] = Side(d["entry_side"]), Product(d["product"])
            self.brackets[d["entry_id"]] = Bracket(**d)
        self._idempotency.update(state.get("idempotency", {}))
