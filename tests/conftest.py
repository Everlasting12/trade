from datetime import datetime, timedelta

import numpy as np
import pytest

from app.core.clock import IST
from app.core.models import Candle, Instrument


def make_candles(closes, tf="1m", start=None, spread=1.0, volume=1000, opens=None, highs=None, lows=None):
    """Build candles from a close series. Consecutive minutes from 09:15 on a weekday."""
    start = start or datetime(2026, 1, 5, 9, 15, tzinfo=IST)  # Monday
    step = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}.get(tf, 1)
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = opens[i] if opens is not None else prev
        h = highs[i] if highs is not None else max(o, c) + spread
        l = lows[i] if lows is not None else min(o, c) - spread
        vol = volume[i] if isinstance(volume, (list, np.ndarray)) else volume
        out.append(Candle(start + timedelta(minutes=step * i), float(o), float(h), float(l), float(c), float(vol), None, tf))
        prev = c
    return out


def random_walk(n=500, seed=7, start=100.0, vol=0.01):
    rng = np.random.default_rng(seed)
    return start * np.exp(np.cumsum(rng.normal(0, vol, n)))


@pytest.fixture
def nifty_fut():
    return Instrument(token=1001, exchange="NFO", tradingsymbol="NIFTYTESTFUT", instrument_type="FUT",
                      segment="NFO-FUT", name="NIFTY", lot_size=75, tick_size=0.05)


@pytest.fixture
def equity():
    return Instrument(token=2001, exchange="NSE", tradingsymbol="TESTEQ", instrument_type="EQ",
                      segment="NSE", lot_size=1, tick_size=0.05)
