"""Live-trading safety: nothing may reach Zerodha unless every guard layer passes."""

from dataclasses import replace
from datetime import datetime

import pytest
from kiteconnect import exceptions as kex

from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.core.clock import IST, SimClock
from app.core.models import Order, OrderRole, OrderStatus, OrderType, Product, Side, Signal, SignalAction, TradingMode
from app.execution.base import BrokerError, OrderRejected
from app.execution.zerodha.broker import ZerodhaBroker
from app.execution.zerodha.guard import LiveTradingGuard
from app.market_data.instruments import InstrumentRegistry
from app.orders.manager import OrderManager
from app.portfolio.portfolio import Portfolio
from app.utils.events import NullEventLog

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=IST)


class FakeKite:
    VARIETY_REGULAR = "regular"
    VALIDITY_DAY = "DAY"
    MARKET_PROTECTION_AUTO = -1

    def __init__(self):
        self.placed = []
        self.book = []
        self.fail_with = None

    def place_order(self, **params):
        if self.fail_with:
            raise self.fail_with
        self.placed.append(params)
        oid = str(1000 + len(self.placed))
        self.book.append({"order_id": oid, "tag": params["tag"], "status": "OPEN", "quantity": params["quantity"],
                          "filled_quantity": 0, "average_price": 0})
        return oid

    def orders(self):
        return self.book


LIVE = replace(Settings(), trading_mode=TradingMode.LIVE, live_trading_enabled=True)


def make(settings, nifty_fut, kill=lambda: False):
    clock = SimClock(T0)
    guard = LiveTradingGuard(settings, clock.now, kill)
    kite = FakeKite()
    broker = ZerodhaBroker(kite, guard, InstrumentRegistry([nifty_fut]).get, clock, max_order_lots=2)
    return guard, kite, broker, clock


def order(key, qty=75, otype=OrderType.MARKET):
    return Order(key, Side.BUY, qty, otype, Product.MIS, OrderRole.ENTRY, "s")


def test_default_settings_never_send(nifty_fut):
    guard, kite, broker, _ = make(Settings(), nifty_fut)
    assert Settings().live_trading_enabled is False and Settings().trading_mode == TradingMode.PAPER
    with pytest.raises(OrderRejected, match="LIVE_TRADING_ENABLED"):
        broker.place_order(order(nifty_fut.key))
    assert kite.placed == []


@pytest.mark.parametrize("settings,expect", [
    (replace(Settings(), live_trading_enabled=True), "TRADING_MODE"),
    (replace(Settings(), trading_mode=TradingMode.LIVE), "LIVE_TRADING_ENABLED"),
])
def test_both_env_switches_required(settings, expect, nifty_fut):
    guard, kite, broker, _ = make(settings, nifty_fut)
    guard.reconciled = True
    with pytest.raises(OrderRejected, match=expect):
        broker.place_order(order(nifty_fut.key))
    assert kite.placed == []


def test_reconciliation_and_arming_required(nifty_fut):
    guard, kite, broker, _ = make(LIVE, nifty_fut)
    with pytest.raises(OrderRejected, match="reconciliation"):
        broker.place_order(order(nifty_fut.key))
    with pytest.raises(PermissionError):
        guard.arm(LIVE_CONFIRM_PHRASE)  # cannot arm before reconciliation
    guard.reconciled = True
    guard.reconcile_issues = ["NFO:X: broker 75 vs internal 0"]
    with pytest.raises(PermissionError, match="mismatch"):
        guard.arm(LIVE_CONFIRM_PHRASE)
    guard.reconcile_issues = []
    with pytest.raises(OrderRejected, match="not armed"):
        broker.place_order(order(nifty_fut.key))
    with pytest.raises(ValueError):
        guard.arm("yes please")
    guard.arm(LIVE_CONFIRM_PHRASE, "tester")
    bid = broker.place_order(order(nifty_fut.key))
    p = kite.placed[-1]
    assert bid == "1001" and p["market_protection"] == -1 and p["tag"] and len(p["tag"]) <= 20
    assert p["exchange"] == "NFO" and p["quantity"] == 75


def test_arming_expires_next_day_and_kill_switch_blocks(nifty_fut):
    killed = {"on": False}
    guard, kite, broker, clock = make(LIVE, nifty_fut, kill=lambda: killed["on"])
    guard.reconciled = True
    guard.arm(LIVE_CONFIRM_PHRASE)
    killed["on"] = True
    with pytest.raises(OrderRejected, match="kill switch"):
        broker.place_order(order(nifty_fut.key))
    killed["on"] = False
    clock.set(T0.replace(day=6))
    with pytest.raises(OrderRejected, match="expired"):
        broker.place_order(order(nifty_fut.key))
    assert not guard.state.armed


def test_lot_cap(nifty_fut):
    guard, kite, broker, _ = make(LIVE, nifty_fut)
    guard.reconciled = True
    guard.arm(LIVE_CONFIRM_PHRASE)
    with pytest.raises(OrderRejected, match="LIVE_MAX_ORDER_LOTS"):
        broker.place_order(order(nifty_fut.key, qty=75 * 3))
    assert kite.placed == []


def test_ambiguous_failure_is_not_retried_and_is_reconciled_by_tag(nifty_fut):
    guard, kite, broker, clock = make(LIVE, nifty_fut)
    guard.reconciled = True
    guard.arm(LIVE_CONFIRM_PHRASE)
    pf = Portfolio(1_000_000)
    oms = OrderManager(broker, pf, InstrumentRegistry([nifty_fut]).get, NullEventLog(clock), clock)

    # The request reached Zerodha (order exists in the book) but the response was lost.
    real_place = kite.place_order

    def lost_response(**params):
        real_place(**params)
        raise kex.NetworkException("read timeout")
    kite.place_order = lost_response
    sig = Signal(SignalAction.BUY, nifty_fut.key, "s", "5m", 25000.0, 24950.0, 25100.0, ts=T0)
    o = oms.submit_entry(sig, 75, Product.MIS)
    assert o.status == OrderStatus.UNKNOWN
    assert len(kite.placed) == 1  # sent exactly once - never retried

    kite.book[0].update(status="COMPLETE", filled_quantity=75, average_price=25001.0)
    oms.reconcile()
    assert o.status == OrderStatus.FILLED and o.broker_order_id == "1001"
    assert pf.positions[nifty_fut.key].quantity == 75
    # Protective orders were placed after the reconciled fill.
    assert {p["order_type"] for p in kite.placed[1:]} == {"SL-M", "LIMIT"}


def test_definite_rejection_raises_order_rejected(nifty_fut):
    guard, kite, broker, _ = make(LIVE, nifty_fut)
    guard.reconciled = True
    guard.arm(LIVE_CONFIRM_PHRASE)
    kite.fail_with = kex.InputException("Insufficient funds")
    with pytest.raises(OrderRejected):
        broker.place_order(order(nifty_fut.key))
    kite.fail_with = kex.NetworkException("timeout")
    with pytest.raises(BrokerError) as ei:
        broker.place_order(order(nifty_fut.key))
    assert not isinstance(ei.value, OrderRejected)


def test_postbacks_deduplicated_and_fill_increments(nifty_fut):
    guard, kite, broker, clock = make(LIVE, nifty_fut)
    guard.reconciled = True
    guard.arm(LIVE_CONFIRM_PHRASE)
    got = []
    broker.set_update_handler(got.append)
    o = order(nifty_fut.key, qty=150, otype=OrderType.LIMIT)
    o.price = 25000.0
    o.broker_order_id = broker.place_order(o)
    base = {"order_id": o.broker_order_id, "tag": o.client_order_id[:20], "quantity": 150}
    broker.on_postback({**base, "status": "OPEN", "filled_quantity": 75, "average_price": 25000.0})
    o.filled_qty, o.avg_fill_price = 75, 25000.0  # what the OMS would record
    broker.on_postback({**base, "status": "OPEN", "filled_quantity": 75, "average_price": 25000.0})  # duplicate
    broker.on_postback({**base, "status": "COMPLETE", "filled_quantity": 150, "average_price": 25001.0})
    broker.on_postback({**base, "status": "OPEN", "filled_quantity": 75, "average_price": 25000.0})  # stale
    assert [u.status for u in got] == [OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED]
    assert got[0].fill_qty == 75 and got[1].fill_qty == 75
    assert got[1].fill_price == pytest.approx(25002.0)  # (150*25001 - 75*25000) / 75
