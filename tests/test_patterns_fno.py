from datetime import date, datetime

import pytest

from app.core.clock import IST
from app.core.models import Instrument
from app.fno.chain import ContractSpec, InstrumentSelector, build_chain, chain_instruments
from app.fno.greeks import bs_price, greeks, implied_vol
from app.market_data.instruments import InstrumentRegistry
from app.patterns import candlestick, chart_patterns, price_action
from tests.conftest import make_candles


def downtrend_then(o, h, l, c):
    closes = [110 - i for i in range(10)]
    cs = make_candles(closes, spread=0.5)
    last = make_candles([c], opens=[o], highs=[h], lows=[l], start=cs[-1].ts.replace(minute=cs[-1].ts.minute + 1))
    return cs + last


def names(res):
    return {p.pattern for p in res}


def test_hammer_in_downtrend_and_structured_output():
    cs = downtrend_then(100.5, 101.0, 96.0, 100.8)
    res = candlestick.detect_at(cs, -1, "5m")
    hammer = next(p for p in res if p.pattern == "hammer")
    assert hammer.direction == "bullish" and hammer.confirmation_required
    assert 0 < hammer.confidence <= 1 and hammer.timeframe == "5m" and hammer.candle_index == len(cs) - 1
    assert "pin_bar" in names(res)


def test_engulfing_and_doji():
    cs = make_candles([100, 101.5], opens=[101, 99.8], highs=[101.2, 101.6], lows=[99.8, 99.7])
    assert "bullish_engulfing" in names(candlestick.detect_at(cs, -1))
    cs = make_candles([100, 100.02], opens=[99, 100.0], highs=[100.5, 101], lows=[98.9, 99])
    assert "doji" in names(candlestick.detect_at(cs, -1))


def test_morning_star():
    cs = make_candles([110 - i for i in range(10)], spread=0.3)
    cs += make_candles([95, 94.8, 99], opens=[100, 95.1, 95.2], highs=[100.2, 95.5, 99.2], lows=[94.8, 94.5, 95.0],
                       start=cs[-1].ts.replace(minute=cs[-1].ts.minute + 1))
    assert "morning_star" in names(candlestick.detect_at(cs, -1))


def test_swings_need_right_side_confirmation():
    closes = [100, 101, 102, 105, 102, 101, 100]
    cs = make_candles(closes, spread=0.2)
    assert any(s.kind == "high" and s.index == 3 for s in price_action.find_swings(cs, 3, 3))
    assert not any(s.kind == "high" for s in price_action.find_swings(cs[:6], 3, 3))  # not yet confirmed


def test_breakout_of_resistance_with_volume():
    closes = ([100, 102, 104, 106, 104, 102, 100, 102, 104, 106, 104, 102, 100, 101, 102, 103, 104, 105]
              + [109])
    vols = [1000] * (len(closes) - 1) + [5000]
    cs = make_candles(closes, spread=0.3, volume=vols)
    snap = price_action.analyze(cs, "5m", swing_strength=2)
    assert snap.nearest_resistance is None or snap.nearest_resistance < 109
    assert snap.has("breakout", "bullish")


def test_double_top():
    up = list(range(80, 120, 2))
    seq = up + [118, 116, 112, 108, 106, 108, 112, 116, 119, 116, 112, 108, 104, 100]
    cs = make_candles(seq, spread=0.4)
    res = chart_patterns.detect(cs + make_candles([99] * 3, start=cs[-1].ts.replace(minute=cs[-1].ts.minute + 1)),
                                "5m", swing_strength=2)
    dt = [p for p in res if p.pattern == "double_top"]
    assert dt and dt[0].direction == "bearish" and dt[0].confirmed


def test_iv_roundtrip_and_greeks():
    p = bs_price(25000, 25100, 7 / 365, 0.14, 0.0, True)
    iv = implied_vol(p, 25000, 25100, 7 / 365, 0.0, True)
    assert iv == pytest.approx(0.14, abs=1e-4)
    g = greeks(p, 25000, 25100, 7 / 365, 0.0, True)
    assert 0 < g.delta < 0.5 and g.gamma > 0 and g.theta < 0 and g.vega > 0
    gp = greeks(bs_price(25000, 25100, 7 / 365, 0.14, 0.0, False), 25000, 25100, 7 / 365, 0.0, False)
    assert gp.delta == pytest.approx(g.delta - 1, abs=1e-3)  # put-call parity on delta (r=0)


@pytest.fixture
def fno_registry():
    exp1, exp2 = date(2026, 1, 6), date(2026, 1, 13)
    items = [Instrument(1, "NFO", "NIFTY26JANFUT", "FUT", "NFO-FUT", "NIFTY", 75, 0.05, date(2026, 1, 27))]
    tok = 10
    for exp in (exp1, exp2):
        for k in range(24500, 25550, 50):
            for t in ("CE", "PE"):
                tok += 1
                items.append(Instrument(tok, "NFO", f"NIFTY{exp:%y%m%d}{k}{t}", t, "NFO-OPT", "NIFTY", 75, 0.05, exp, float(k)))
    return InstrumentRegistry(items)


def test_selector_moneyness(fno_registry):
    sel = InstrumentSelector(fno_registry)
    today = date(2026, 1, 5)
    assert sel.select(ContractSpec("NIFTY", "FUT"), today=today).tradingsymbol == "NIFTY26JANFUT"
    atm = sel.select(ContractSpec("NIFTY", "CE", "near", "ATM"), spot=25020, today=today)
    assert atm.strike == 25000 and atm.expiry == date(2026, 1, 6)
    assert sel.select(ContractSpec("NIFTY", "CE", "near", "ITM2"), spot=25020, today=today).strike == 24900
    assert sel.select(ContractSpec("NIFTY", "PE", "next", "OTM1"), spot=25020, today=today).strike == 24950
    assert fno_registry.resolve("FUT:NIFTY", today).key == "NFO:NIFTY26JANFUT"


def test_chain_build(fno_registry):
    now = datetime(2026, 1, 5, 11, 0, tzinfo=IST)
    contracts = chain_instruments(fno_registry, "NIFTY", date(2026, 1, 6), 25020, width=3, today=now.date())
    assert len(contracts) == 14
    quotes = {}
    for c in contracts:
        px = bs_price(25020, c.strike, 1.2 / 365, 0.13, 0, c.instrument_type == "CE")
        quotes[c.key] = {"last_price": round(px, 1), "oi": 1000 if c.instrument_type == "CE" else 1500, "volume": 10,
                         "depth": {"buy": [{"price": round(px - 0.5, 1)}], "sell": [{"price": round(px + 0.5, 1)}]}}
    ch = build_chain(contracts, quotes, 25020, now)
    assert ch["atm_strike"] == 25000 and ch["pcr_oi"] == pytest.approx(1.5)
    atm = next(r for r in ch["rows"] if r["strike"] == 25000)
    assert atm["ce_iv"] == pytest.approx(13.0, abs=1.5) and atm["ce_delta"] > 0 > atm["pe_delta"]
