from datetime import datetime, timedelta

import pytest

from app.core.clock import IST, SimClock
from app.core.models import (AccountBalance, Candle, OrderRole, OrderStatus, OrderType, Product, Signal,
                             SignalAction)
from app.execution.base import BrokerError
from app.execution.paper.broker import PaperBroker, PaperConfig
from app.market_data.instruments import InstrumentRegistry
from app.orders.manager import OrderManager
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskConfig, RiskInputs, RiskManager, StrategyLimits
from app.utils.events import NullEventLog

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=IST)


@pytest.fixture
def oms(nifty_fut):
    reg = InstrumentRegistry([nifty_fut])
    clock = SimClock(T0)
    broker = PaperBroker(reg.get, PaperConfig(capital=2_000_000, slippage_bps=0, partial_fills=False), clock=clock)
    pf = Portfolio(2_000_000)
    m = OrderManager(broker, pf, reg.get, NullEventLog(clock), clock)
    broker.on_quote(nifty_fut.key, 25000)
    return m, broker, pf, nifty_fut.key, clock


def sig(key, action="BUY", entry=25000.0, sl=24950.0, tgt=25100.0, ts=T0, strategy="s1"):
    return Signal(SignalAction(action), key, strategy, "5m", entry, sl, tgt, ts=ts)


def test_entry_creates_bracket_and_oco(oms):
    m, b, pf, key, clock = oms
    e = m.submit_entry(sig(key), 75, Product.MIS)
    b.on_quote(key, 25000)
    assert e.status == OrderStatus.FILLED and pf.positions[key].quantity == 75
    br = m.bracket_for(key)
    sl, tgt = m.orders[br.sl_id], m.orders[br.target_id]
    assert sl.order_type == OrderType.SLM and sl.trigger_price == 24950 and sl.side.value == "SELL"
    assert tgt.order_type == OrderType.LIMIT and tgt.price == 25100
    b.on_quote(key, 25101)  # target hit
    assert tgt.status == OrderStatus.FILLED
    assert sl.status == OrderStatus.CANCELLED  # OCO
    assert key not in pf.positions
    t = pf.trades[-1]
    assert t.exit_reason == "target" and t.gross_pnl == pytest.approx(101 * 75)  # marketable limit fills at the better price


def test_duplicate_signal_and_pending_entry_prevented(oms):
    m, b, pf, key, clock = oms
    s = sig(key)
    assert m.submit_entry(s, 75, Product.MIS) is not None
    assert m.submit_entry(sig(key), 75, Product.MIS) is None          # same strategy/instrument/ts
    assert m.submit_entry(sig(key, ts=T0 + timedelta(minutes=5)), 75, Product.MIS) is None  # entry still working


def test_stop_rejected_triggers_market_exit(oms):
    m, b, pf, key, clock = oms
    m.submit_entry(sig(key, sl=24990.0), 75, Product.MIS)
    b.on_quote(key, 24980)  # fills at 24980; SL trigger 24990 is already above LTP -> rejected
    assert any(o.role == OrderRole.STOP_LOSS and o.status == OrderStatus.REJECTED for o in m.orders.values())
    exits = [o for o in m.orders.values() if o.role == OrderRole.EXIT]
    assert exits, "OMS must exit an unprotected position"
    b.on_quote(key, 24970)
    assert key not in pf.positions


def test_partial_entry_resizes_protection(nifty_fut):
    reg = InstrumentRegistry([nifty_fut])
    clock = SimClock(T0)
    b = PaperBroker(reg.get, PaperConfig(capital=5_000_000, slippage_bps=0), clock=clock)
    pf = Portfolio(5_000_000)
    m = OrderManager(b, pf, reg.get, NullEventLog(clock), clock)
    key = nifty_fut.key
    b.on_quote(key, 25000, 24999, 25000, 75, 75)
    m.submit_entry(sig(key), 225, Product.MIS)
    b.on_quote(key, 25000, 24999, 25000, 75, 75)
    br = m.bracket_for(key)
    assert m.orders[br.sl_id].quantity == 75
    b.on_quote(key, 25000, 24999, 25000, 150, 150)
    assert pf.positions[key].quantity == 225
    assert m.orders[br.sl_id].quantity == 225 and m.orders[br.target_id].quantity == 225


def test_exit_and_flatten(oms):
    m, b, pf, key, clock = oms
    m.submit_entry(sig(key), 75, Product.MIS)
    b.on_quote(key, 25000)
    m.flatten_all("kill")
    b.on_quote(key, 25010)
    assert key not in pf.positions
    assert not m.working()
    assert pf.trades[-1].exit_reason == "kill"


def test_ambiguous_send_marked_unknown_and_reconciled(oms):
    m, b, pf, key, clock = oms
    orig = b.place_order

    def boom(order):
        raise BrokerError("timeout")
    b.place_order = boom
    o = m.submit_entry(sig(key), 75, Product.MIS)
    assert o.status == OrderStatus.UNKNOWN
    b.place_order = orig
    m.reconcile()  # not at broker -> rejected, never auto re-sent
    assert o.status == OrderStatus.REJECTED and len(b.orders) == 0


# ------------------------------------------------------------------ risk

def inputs(inst, pf, now=T0, avail=1_000_000, **kw):
    return RiskInputs(now, inst, pf, AccountBalance(avail, avail, 0), 25000, 0.0, frozenset(),
                      lambda q: q * 25000 * 0.15, True, **kw)


def test_position_sizing_from_stop_distance(nifty_fut):
    rm = RiskManager(RiskConfig(max_risk_per_trade=8000, max_position_lots=10, max_daily_loss=50_000))
    d = rm.evaluate(sig(nifty_fut.key, sl=24950), inputs(nifty_fut, Portfolio(1_000_000)))
    assert d.approved and d.quantity == 150  # 8000 / (50 * 75) = 2.13 -> 2 lots


def test_risk_rejections(nifty_fut):
    key = nifty_fut.key
    pf = Portfolio(1_000_000)
    rm = RiskManager(RiskConfig(max_risk_per_trade=500))
    d = rm.evaluate(sig(key, sl=24950), inputs(nifty_fut, pf))
    assert not d.approved and any("position_size" in r for r in d.reasons)  # one lot risks 3750 > 500

    rm = RiskManager(RiskConfig(max_risk_per_trade=10_000, max_daily_loss=50_000))
    assert not rm.evaluate(sig(key, sl=None), inputs(nifty_fut, pf)).approved           # stop required
    assert not rm.evaluate(sig(key, sl=25050), inputs(nifty_fut, pf)).approved          # stop on wrong side
    assert not rm.evaluate(sig(key, tgt=25020), inputs(nifty_fut, pf)).approved          # R:R 0.4 < 1
    late = T0.replace(hour=15, minute=5)
    assert not rm.evaluate(sig(key), inputs(nifty_fut, pf, now=late)).approved           # entry window
    stale = RiskInputs(T0, nifty_fut, pf, AccountBalance(1e6, 1e6, 0), 25000, 120.0)
    assert not rm.evaluate(sig(key), stale).approved                                     # stale data
    rm.trip_kill_switch("test", T0)
    d = rm.evaluate(sig(key), inputs(nifty_fut, pf))
    assert not d.approved and any("kill_switch" in r for r in d.reasons)


def test_exit_always_allowed_even_with_kill_switch(nifty_fut, oms):
    m, b, pf, key, clock = oms
    m.submit_entry(sig(key), 75, Product.MIS)
    b.on_quote(key, 25000)
    rm = RiskManager()
    rm.trip_kill_switch("x", T0)
    d = rm.evaluate(sig(key, "EXIT"), inputs(nifty_fut, pf))
    assert d.approved and d.quantity == 75


def test_daily_loss_halt_and_limits(nifty_fut, oms):
    m, b, pf, key, clock = oms
    rm = RiskManager(RiskConfig(max_daily_loss=2000, max_risk_per_trade=10_000, max_trades_per_day=1,
                                strategy_limits={"s1": StrategyLimits(max_open_positions=0)}))
    d = rm.evaluate(sig(key), inputs(nifty_fut, pf))
    assert any("strategy_open_positions" in r for r in d.reasons)
    m.submit_entry(sig(key, sl=24900), 75, Product.MIS)
    b.on_quote(key, 25000)
    b.on_quote(key, 24960)
    pf.mark(key, 24960)  # unrealised -3000 (the engine marks on every tick)
    assert rm.check_breaches(T0, pf) is not None
    d = rm.evaluate(sig(key, ts=T0 + timedelta(minutes=1)), inputs(nifty_fut, pf))
    assert not d.approved
    assert {"daily_halt", "max_trades_per_day", "no_existing_position", "daily_loss"} <= \
        {c.check for c in d.checks if not c.passed}
