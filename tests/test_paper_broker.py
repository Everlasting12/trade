from datetime import datetime

import pytest

from app.core.clock import IST, SimClock
from app.core.models import Candle, Order, OrderRole, OrderStatus, OrderType, Product, Side
from app.execution.charges import ChargesModel
from app.execution.paper.broker import PaperBroker, PaperConfig
from app.market_data.instruments import InstrumentRegistry

T0 = datetime(2026, 1, 5, 10, 0, tzinfo=IST)


@pytest.fixture
def setup(nifty_fut):
    reg = InstrumentRegistry([nifty_fut])
    clock = SimClock(T0)
    updates = []
    b = PaperBroker(reg.get, PaperConfig(capital=1_000_000, slippage_bps=0), clock=clock)
    b.set_update_handler(updates.append)
    return b, updates, nifty_fut.key


def order(key, side=Side.BUY, qty=75, otype=OrderType.MARKET, price=None, trigger=None):
    return Order(key, side, qty, otype, Product.MIS, OrderRole.ENTRY, "t", price=price, trigger_price=trigger)


def test_market_order_fills_on_next_quote_at_ask(setup):
    b, ups, key = setup
    b.on_quote(key, 100, 99.9, 100.1, 1000, 1000)
    o = order(key)
    b.place_order(o)
    assert o.status == OrderStatus.OPEN and o.filled_qty == 0
    b.on_quote(key, 100.5, 100.4, 100.6, 1000, 1000)
    assert o.status == OrderStatus.FILLED and o.avg_fill_price == pytest.approx(100.6)
    assert b.positions[key].qty == 75


def test_slippage_is_adverse(nifty_fut):
    reg = InstrumentRegistry([nifty_fut])
    b = PaperBroker(reg.get, PaperConfig(slippage_bps=10), clock=SimClock(T0))
    b.on_quote(nifty_fut.key, 1000)
    buy, sell = order(nifty_fut.key), order(nifty_fut.key, Side.SELL)
    b.place_order(buy)
    b.on_quote(nifty_fut.key, 1000)
    b.place_order(sell)
    b.on_quote(nifty_fut.key, 1000)
    assert buy.avg_fill_price == pytest.approx(1001.0)
    assert sell.avg_fill_price == pytest.approx(999.0)


def test_partial_fills_limited_by_top_of_book(setup):
    b, ups, key = setup
    b.on_quote(key, 100, 99.9, 100.0, 100, 100)
    o = order(key, qty=300)
    b.place_order(o)
    b.on_quote(key, 100, 99.9, 100.0, 150, 150)
    assert o.status == OrderStatus.PARTIALLY_FILLED and o.filled_qty == 150
    b.on_quote(key, 100, 99.9, 100.0, 1000, 1000)
    assert o.status == OrderStatus.FILLED and o.filled_qty == 300


def test_limit_and_stop_logic_in_tick_mode(setup):
    b, ups, key = setup
    b.on_quote(key, 100, 99.95, 100.05)
    lim = order(key, otype=OrderType.LIMIT, price=99.5)
    b.place_order(lim)
    b.on_quote(key, 99.8, 99.75, 99.85)
    assert lim.filled_qty == 0
    b.on_quote(key, 99.4, 99.35, 99.45)
    assert lim.status == OrderStatus.FILLED and lim.avg_fill_price == pytest.approx(99.45)
    stop = order(key, Side.SELL, otype=OrderType.SLM, trigger=98.0)
    b.place_order(stop)
    assert stop.status == OrderStatus.TRIGGER_PENDING
    b.on_quote(key, 98.5, 98.45, 98.55)
    assert stop.filled_qty == 0
    b.on_quote(key, 97.9, 97.85, 97.95)
    assert stop.status == OrderStatus.FILLED and b.positions[key].qty == 0
    assert b.positions[key].realized == pytest.approx((97.85 - 99.45) * 75)


def test_validation_rejections(setup):
    b, ups, key = setup
    b.on_quote(key, 100)
    for o, why in [(order(key, qty=50), "lot size"), (order(key, otype=OrderType.LIMIT, price=100.03), "tick"),
                   (order(key, Side.SELL, otype=OrderType.SLM, trigger=101), "crossed"),
                   (order(key, qty=75 * 2000), "margin")]:
        b.place_order(o)
        assert o.status == OrderStatus.REJECTED and why in o.reject_reason.lower(), o.reject_reason


def test_bar_mode_gaps_and_stop_before_target(setup):
    b, ups, key = setup
    bar = lambda o, h, l, c: Candle(T0, o, h, l, c, 1e9, None, "1m")
    entry = order(key)
    b.on_quote(key, 100)
    b.place_order(entry)
    b.on_bar(key, bar(101, 103, 100, 102))
    assert entry.avg_fill_price == pytest.approx(101)  # next bar open
    sl = order(key, Side.SELL, otype=OrderType.SLM, trigger=99.0)
    tgt = order(key, Side.SELL, otype=OrderType.LIMIT, price=104.0)
    b.place_order(sl)
    b.place_order(tgt)
    b.on_bar(key, bar(102, 105, 98, 100))  # both levels inside the bar
    assert sl.status == OrderStatus.FILLED and sl.avg_fill_price == pytest.approx(99.0)
    # (OCO cancellation of the target is the OMS's job; the broker alone may fill both)


def test_bar_mode_gap_through_stop_fills_at_open(setup):
    b, ups, key = setup
    b.on_quote(key, 100)
    e = order(key)
    b.place_order(e)
    b.on_bar(key, Candle(T0, 100, 100, 100, 100, 1e9, None, "1m"))
    sl = order(key, Side.SELL, otype=OrderType.SLM, trigger=98.0)
    b.place_order(sl)
    b.on_bar(key, Candle(T0, 95, 96, 94, 95, 1e9, None, "1m"))
    assert sl.avg_fill_price == pytest.approx(95.0)


def test_charges_futures_brokerage_capped_per_order(nifty_fut):
    m = ChargesModel()
    big = m.for_fill(nifty_fut, Product.NRML, Side.SELL, 750, 25000)   # turnover 1.875 cr
    assert big.brokerage == pytest.approx(20.0)
    assert big.stt > 0 and big.stamp == 0
    first = m.for_fill(nifty_fut, Product.NRML, Side.BUY, 75, 25000)
    second = m.for_fill(nifty_fut, Product.NRML, Side.BUY, 75, 25000, order_turnover_before=75 * 25000)
    assert first.brokerage + second.brokerage == pytest.approx(20.0)
    assert first.stamp > 0


def test_state_roundtrip(setup):
    b, ups, key = setup
    b.on_quote(key, 100)
    e = order(key)
    b.place_order(e)
    b.on_quote(key, 100)
    b.place_order(order(key, Side.SELL, otype=OrderType.LIMIT, price=110.0))
    st = b.to_state()
    b2 = PaperBroker(b._instrument, b.cfg, clock=b.clock)
    b2.load_state(st)
    assert b2.positions[key].qty == 75 and len([w for w in b2.orders.values()]) == 1
    assert b2.get_account_balance().cash == pytest.approx(b.get_account_balance().cash)
