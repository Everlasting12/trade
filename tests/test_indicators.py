import numpy as np
import pandas as pd
import pytest

from app.indicators.base import IndicatorSpec
from app.indicators.engine import IndicatorEngine
from app.indicators.library import (ADX, ATR, CCI, EMA, MACD, OBV, RSI, SMA, VWAP, WMA, BollingerBands,
                                    Stochastic, Supertrend, create)
from tests.conftest import make_candles, random_walk


@pytest.fixture
def candles():
    return make_candles(random_walk(400))


@pytest.fixture
def df(candles):
    return pd.DataFrame([c.as_dict() for c in candles])


def _cmp(ours, ref, start):
    ours = np.array([np.nan if v is None else v for v in ours], dtype=float)
    ref = np.asarray(ref, dtype=float)
    assert np.all(np.isnan(ours[:start]))
    np.testing.assert_allclose(ours[start:], ref[start:], rtol=1e-9, atol=1e-9)


def test_sma(candles, df):
    _cmp(SMA.compute(candles, period=20), df.close.rolling(20).mean(), 19)


def test_ema_seeded_with_sma(candles, df):
    n = 20
    ref = np.full(len(df), np.nan)
    ref[n - 1] = df.close[:n].mean()
    a = 2 / (n + 1)
    for i in range(n, len(df)):
        ref[i] = a * df.close[i] + (1 - a) * ref[i - 1]
    _cmp(EMA.compute(candles, period=n), ref, n - 1)


def test_wma(candles, df):
    w = np.arange(1, 11)
    ref = df.close.rolling(10).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)
    _cmp(WMA.compute(candles, period=10), ref, 9)


def _wilder(x: pd.Series, n: int) -> np.ndarray:
    x = x.to_numpy()
    out = np.full(len(x), np.nan)
    first = np.where(~np.isnan(x))[0][0]
    out[first + n - 1] = x[first:first + n].mean()
    for i in range(first + n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def test_rsi_wilder(candles, df):
    ch = df.close.diff()
    g, l = _wilder(ch.clip(lower=0), 14), _wilder((-ch).clip(lower=0), 14)
    _cmp(RSI.compute(candles, period=14), 100 - 100 / (1 + g / l), 14)


def test_atr_wilder(candles, df):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = df.high[0] - df.low[0]
    _cmp(ATR.compute(candles, period=14), _wilder(tr, 14), 13)


def test_bbands_population_std(candles, df):
    vals = BollingerBands.compute(candles, period=20, mult=2)
    mid = df.close.rolling(20).mean()
    sd = df.close.rolling(20).std(ddof=0)
    _cmp([v and v["upper"] for v in vals], mid + 2 * sd, 19)
    _cmp([v and v["lower"] for v in vals], mid - 2 * sd, 19)


def test_macd_consistent_with_emas(candles):
    vals = MACD.compute(candles, fast=12, slow=26, signal=9)
    f, s = EMA.compute(candles, period=12), EMA.compute(candles, period=26)
    first = next(i for i, v in enumerate(vals) if v)
    assert first == 25 + 8
    for i in range(first, len(vals)):
        assert vals[i]["macd"] == pytest.approx(f[i] - s[i])
        assert vals[i]["hist"] == pytest.approx(vals[i]["macd"] - vals[i]["signal"])


def test_cci_and_obv(candles, df):
    tp = (df.high + df.low + df.close) / 3
    m = tp.rolling(20).mean()
    md = tp.rolling(20).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    _cmp(CCI.compute(candles, period=20), (tp - m) / (0.015 * md), 19)
    obv = (np.sign(df.close.diff()).fillna(0) * df.volume).cumsum()
    _cmp(OBV.compute(candles), obv, 0)


def test_bounded_oscillators(candles):
    for v in RSI.compute(candles):
        assert v is None or 0 <= v <= 100
    for v in Stochastic.compute(candles):
        assert v is None or (0 <= v["k"] <= 100 and 0 <= v["d"] <= 100)
    for v in ADX.compute(candles):
        assert v is None or (0 <= v["adx"] <= 100)


def test_supertrend_flips_with_trend():
    up = make_candles(list(np.linspace(100, 150, 60)) + list(np.linspace(150, 100, 60)))
    vals = Supertrend.compute(up, period=10, multiplier=3)
    assert vals[55]["direction"] == 1 and vals[55]["value"] < up[55].close
    assert vals[-1]["direction"] == -1 and vals[-1]["value"] > up[-1].close


def test_vwap_resets_each_session(candles):
    from datetime import timedelta
    day2 = make_candles([50.0] * 5, start=candles[0].ts + timedelta(days=1))
    vals = VWAP.compute(candles[:5] + day2)
    assert vals[-1] == pytest.approx(50.0)  # only day-2 candles count


def test_incremental_equals_batch_via_engine(candles):
    eng = IndicatorEngine()
    spec = IndicatorSpec.of("rsi", "1m", period=14)
    eng.register(spec, warmup=candles[:200])
    for c in candles[200:]:
        eng.on_candle(c)
    batch = RSI.compute(candles, period=14)
    assert eng.value(spec) == pytest.approx(batch[-1])
    assert eng.value(spec, offset=1) == pytest.approx(batch[-2])


def test_unknown_indicator():
    with pytest.raises(ValueError):
        create("nope")
