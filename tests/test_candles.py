from datetime import datetime, timedelta

from app.core.clock import IST
from app.core.models import Tick
from app.market_data import timeframes as tfm
from app.market_data.candles import MultiTimeframeAggregator
from tests.conftest import make_candles

D = datetime(2026, 1, 5, tzinfo=IST)


def at(h, m, s=0):
    return D.replace(hour=h, minute=m, second=s)


def test_bucket_alignment_to_session_open():
    assert tfm.bucket_start(at(9, 17, 30), "3m") == at(9, 15)
    assert tfm.bucket_start(at(9, 18), "3m") == at(9, 18)
    assert tfm.bucket_start(at(10, 14, 59), "1h") == at(9, 15)
    assert tfm.bucket_start(at(10, 15), "1h") == at(10, 15)
    # last hourly bucket is clipped to the 15:30 close
    assert tfm.bucket_end(at(15, 15), "1h") == at(15, 30)
    assert tfm.bucket_start(at(9, 10), "5m") == at(9, 15)  # pre-open ticks join the first bucket


def test_ticks_build_candles_with_volume_deltas_and_dedupe():
    agg = MultiTimeframeAggregator(1, ["1m", "5m"])
    ticks = [Tick(1, at(9, 15, 1), 100, 1000), Tick(1, at(9, 15, 1), 100, 1000),  # duplicate
             Tick(1, at(9, 15, 20), 102, 1500), Tick(1, at(9, 15, 40), 99, 1700),
             Tick(1, at(9, 15, 10), 500, 1800),  # out of order -> dropped
             Tick(1, at(9, 16, 0), 101, 2000)]
    closed = []
    for t in ticks:
        closed += agg.on_tick(t)
    assert agg.duplicate_ticks == 1 and agg.late_ticks == 1
    assert len(closed) == 1
    c = closed[0]
    assert (c.timeframe, c.open, c.high, c.low, c.close) == ("1m", 100, 102, 99, 99)
    assert c.volume == 700  # first tick of day contributes 0, then 500 + 200


def test_flush_closes_candle_on_timer_and_rejects_late_ticks():
    agg = MultiTimeframeAggregator(1, ["1m"])
    agg.on_tick(Tick(1, at(9, 15, 5), 100, 10))
    assert agg.flush(at(9, 16, 1), grace_seconds=2) == []
    out = agg.flush(at(9, 16, 3), grace_seconds=2)
    assert len(out) == 1 and out[0].ts == at(9, 15)
    assert agg.on_tick(Tick(1, at(9, 15, 59), 105, 20)) == []  # bucket already closed


def test_base_candles_resample_to_higher_timeframes_exactly_once():
    bars = make_candles([100 + i for i in range(15)])  # 09:15..09:29
    agg = MultiTimeframeAggregator(1, ["5m", "15m"])
    closed = [c for b in bars for c in agg.on_base_candle(b)]
    five = [c for c in closed if c.timeframe == "5m"]
    fifteen = [c for c in closed if c.timeframe == "15m"]
    assert [c.ts for c in five] == [at(9, 15), at(9, 20), at(9, 25)]
    assert five[0].open == bars[0].open and five[0].close == bars[4].close
    assert five[0].high == max(b.high for b in bars[:5])
    assert len(fifteen) == 1 and fifteen[0].volume == sum(b.volume for b in bars)
    # 5m candle is emitted together with the 1m candle that completes it (09:19 bar), not before
    idx = closed.index(five[0])
    assert closed[idx - 1].ts == at(9, 19)
