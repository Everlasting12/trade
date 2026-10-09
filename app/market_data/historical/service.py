"""Historical OHLC data: Kite historical API (chunked + cached), CSV import, and synthetic data.

Kite limits: 3 requests/second and a maximum date range per request that depends on the interval
(see timeframes.KITE_MAX_DAYS). The historical API is a paid add-on on Kite Connect.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from app.core.clock import IST, SESSION_CLOSE, SESSION_OPEN, to_ist
from app.core.models import Candle
from app.market_data import timeframes as tfm


class HistoricalDataService:
    def __init__(self, kite: Any, repo: Any = None, min_interval_s: float = 0.35):
        self.kite = kite
        self.repo = repo
        self.min_interval_s = min_interval_s
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def fetch(self, token: int, timeframe: str, start: datetime, end: datetime, oi: bool = False,
              use_cache: bool = True) -> list[Candle]:
        start, end = to_ist(start), to_ist(end)
        interval = tfm.KITE_INTERVAL[tfm.validate(timeframe)]
        if use_cache and self.repo is not None:
            cached = self.repo.load_candles(token, timeframe, start, end)
            # Cache is trusted only for complete past days; always refetch if it doesn't cover the range.
            if cached and cached[0].ts.date() <= start.date() and cached[-1].ts.date() >= min(end.date(), date.today() - timedelta(days=1)) \
                    and end.date() < date.today():
                return cached
        out: list[Candle] = []
        step = timedelta(days=tfm.KITE_MAX_DAYS[timeframe])
        cur = start
        while cur <= end:
            chunk_end = min(cur + step, end)
            self._throttle()
            rows = self.kite.historical_data(token, cur, chunk_end, interval, oi=oi)
            for r in rows:
                out.append(Candle(to_ist(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                                  float(r["close"]), float(r.get("volume") or 0), r.get("oi"), timeframe))
            cur = chunk_end + timedelta(seconds=1)
        dedup: dict[datetime, Candle] = {c.ts: c for c in out}
        candles = [dedup[k] for k in sorted(dedup)]
        if self.repo is not None:
            now = datetime.now(IST)
            complete = [c for c in candles if tfm.bucket_end(c.ts, timeframe) <= now]
            self.repo.save_candles(token, timeframe, complete)
        return candles


def candles_from_frame(df: pd.DataFrame, timeframe: str) -> list[Candle]:
    """CSV/DataFrame import. Needs columns date|ts|datetime, open, high, low, close[, volume, oi].
    Naive timestamps are assumed to be IST."""
    cols = {c.lower().strip(): c for c in df.columns}
    tcol = next((cols[c] for c in ("date", "ts", "datetime", "timestamp", "time") if c in cols), None)
    if tcol is None:
        raise ValueError("CSV needs a date/datetime column")
    for need in ("open", "high", "low", "close"):
        if need not in cols:
            raise ValueError(f"CSV missing column {need!r}")
    ts = pd.to_datetime(df[tcol])
    out = []
    for i in range(len(df)):
        t = ts.iloc[i].to_pydatetime()
        out.append(Candle(to_ist(t), float(df[cols["open"]].iloc[i]), float(df[cols["high"]].iloc[i]),
                          float(df[cols["low"]].iloc[i]), float(df[cols["close"]].iloc[i]),
                          float(df[cols["volume"]].iloc[i]) if "volume" in cols else 0.0,
                          float(df[cols["oi"]].iloc[i]) if "oi" in cols else None, timeframe))
    out.sort(key=lambda c: c.ts)
    return out


def synthetic_candles(start: date, days: int, timeframe: str = "1m", start_price: float = 25000.0,
                      annual_vol: float = 0.15, drift: float = 0.0, seed: int = 42,
                      base_volume: float = 50_000) -> list[Candle]:
    """Random-walk OHLCV for demos and tests ONLY (regime-switching drift so trend strategies trigger).
    Results on synthetic data say nothing about real-market performance."""
    rng = np.random.default_rng(seed)
    secs = tfm.seconds(timeframe) if timeframe != "1d" else 22500
    per_year = 252 * 22500 / secs
    sigma = annual_vol / np.sqrt(per_year)
    out: list[Candle] = []
    price = start_price
    d = start
    produced = 0
    regime = 0.0
    while produced < days:
        if d.weekday() < 5:
            if timeframe == "1d":
                stamps = [datetime(d.year, d.month, d.day, tzinfo=IST)]
            else:
                t = datetime.combine(d, SESSION_OPEN, IST)
                close = datetime.combine(d, SESSION_CLOSE, IST)
                stamps = []
                while t < close:
                    stamps.append(t)
                    t += timedelta(seconds=secs)
            if timeframe != "1d":
                price *= float(np.exp(rng.normal(0, sigma * 3)))  # overnight gap
            n = len(stamps)
            for i, ts in enumerate(stamps):
                if rng.random() < 0.02:
                    regime = rng.normal(drift, sigma * 0.15)
                ret = rng.normal(regime, sigma)
                o = price
                c = price * float(np.exp(ret))
                wick = abs(rng.normal(0, sigma * 0.6))
                h = max(o, c) * (1 + wick)
                l = min(o, c) * (1 - abs(rng.normal(0, sigma * 0.6)))
                u = abs(i / max(n - 1, 1) - 0.5) * 2  # U-shaped intraday volume
                vol = base_volume * (0.6 + 0.8 * u) * float(np.exp(rng.normal(0, 0.4))) * (1 + 5 * abs(ret) / sigma * 0.1)
                out.append(Candle(ts, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(vol), None, timeframe))
                price = c
            produced += 1
        d += timedelta(days=1)
    return out
