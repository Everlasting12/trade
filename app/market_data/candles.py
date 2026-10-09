"""Tick -> candle aggregation and in-memory candle storage.

Design points
- Every timeframe is built directly from ticks (live) or from closed base candles (backtest /
  historical replay), so higher-timeframe candles are exact and never use unfinished data.
- A candle is emitted exactly once, when its bucket is complete: either a tick/candle for a later
  bucket arrives, or the timer calls `flush()` after the bucket end (+ grace for late ticks).
- Duplicate ticks and out-of-order/late ticks for an already-closed bucket are dropped and counted.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from typing import Iterable

import pandas as pd

from app.core.clock import to_ist
from app.core.models import Candle, Tick
from app.market_data import timeframes as tfm


class CandleBuilder:
    """Builds candles of one timeframe for one instrument."""

    def __init__(self, timeframe: str):
        self.tf = tfm.validate(timeframe)
        self.current: Candle | None = None
        self.current_end: datetime | None = None
        self.last_closed_start: datetime | None = None

    def _close(self) -> Candle | None:
        c, self.current, self.current_end = self.current, None, None
        if c is not None:
            self.last_closed_start = c.ts
        return c

    def _accept(self, start: datetime) -> tuple[bool, Candle | None]:
        """Returns (accept, closed_candle). Rejects data for buckets already closed."""
        if self.last_closed_start is not None and start <= self.last_closed_start:
            return False, None
        closed = None
        if self.current is not None and start != self.current.ts:
            if start < self.current.ts:
                return False, None
            closed = self._close()
        return True, closed

    def add_price(self, ts: datetime, price: float, volume: float = 0.0, oi: float | None = None) -> list[Candle]:
        start = tfm.bucket_start(ts, self.tf)
        ok, closed = self._accept(start)
        if not ok:
            return []
        c = self.current
        if c is None:
            self.current = Candle(start, price, price, price, price, volume, oi, self.tf)
            self.current_end = tfm.bucket_end(start, self.tf)
        else:
            c.high = max(c.high, price)
            c.low = min(c.low, price)
            c.close = price
            c.volume += volume
            if oi is not None:
                c.oi = oi
        return [closed] if closed else []

    def add_candle(self, base: Candle, base_end: datetime) -> list[Candle]:
        """Merge a *closed* lower-timeframe candle. Emits this timeframe's candle as soon as the
        base candle completes the bucket, so there is no extra delay and no look-ahead."""
        start = tfm.bucket_start(base.ts, self.tf)
        ok, closed = self._accept(start)
        if not ok:
            return []
        out = [closed] if closed else []
        c = self.current
        if c is None:
            self.current = Candle(start, base.open, base.high, base.low, base.close, base.volume, base.oi, self.tf)
            self.current_end = tfm.bucket_end(start, self.tf)
        else:
            c.high = max(c.high, base.high)
            c.low = min(c.low, base.low)
            c.close = base.close
            c.volume += base.volume
            if base.oi is not None:
                c.oi = base.oi
        if self.current_end is not None and base_end >= self.current_end:
            out.append(self._close())
        return out

    def flush(self, now: datetime) -> list[Candle]:
        if self.current is not None and self.current_end is not None and now >= self.current_end:
            return [self._close()]
        return []


class MultiTimeframeAggregator:
    """All timeframes for a single instrument."""

    def __init__(self, instrument_token: int, timeframes: Iterable[str], base_timeframe: str = "1m"):
        self.token = instrument_token
        self.base_tf = tfm.validate(base_timeframe)
        tfs = set(timeframes) | {self.base_tf}
        self.builders = {tf: CandleBuilder(tf) for tf in sorted(tfs, key=tfm.seconds)}
        self._last_tick_key: tuple | None = None
        self._last_tick_ts: datetime | None = None
        self._last_cum_volume: int | None = None
        self._volume_day = None
        self.duplicate_ticks = 0
        self.late_ticks = 0

    def add_timeframe(self, tf: str) -> None:
        if tf not in self.builders:
            self.builders[tf] = CandleBuilder(tf)
            self.builders = dict(sorted(self.builders.items(), key=lambda kv: tfm.seconds(kv[0])))

    def on_tick(self, tick: Tick) -> list[Candle]:
        ts = to_ist(tick.ts)
        key = (ts, tick.ltp, tick.cumulative_volume)
        if key == self._last_tick_key:
            self.duplicate_ticks += 1
            return []
        if self._last_tick_ts is not None and ts < self._last_tick_ts:
            self.late_ticks += 1
            return []
        self._last_tick_key, self._last_tick_ts = key, ts

        vol = 0.0
        if tick.cumulative_volume is not None:
            if self._volume_day != ts.date() or self._last_cum_volume is None:
                # First tick seen today: we don't know how much of the day volume belongs to this bucket.
                self._volume_day = ts.date()
            elif tick.cumulative_volume >= self._last_cum_volume:
                vol = float(tick.cumulative_volume - self._last_cum_volume)
            self._last_cum_volume = tick.cumulative_volume

        closed: list[Candle] = []
        for b in self.builders.values():
            closed.extend(b.add_price(ts, tick.ltp, vol, tick.oi))
        return closed

    def on_base_candle(self, candle: Candle) -> list[Candle]:
        """Feed a closed base-timeframe candle (backtest / replay). Returns all candles closed by it,
        including the base candle itself, ordered smallest timeframe first."""
        candle.ts = to_ist(candle.ts)
        candle.timeframe = self.base_tf
        base_b = self.builders[self.base_tf]
        start = tfm.bucket_start(candle.ts, self.base_tf)
        if base_b.last_closed_start is not None and start <= base_b.last_closed_start:
            self.late_ticks += 1
            return []
        base_b.last_closed_start = start
        out = [candle]
        end = tfm.bucket_end(candle.ts, self.base_tf)
        for tf, b in self.builders.items():
            if tf != self.base_tf:
                out.extend(b.add_candle(candle, end))
        return out

    def flush(self, now: datetime, grace_seconds: float = 2.0) -> list[Candle]:
        now = to_ist(now) - timedelta(seconds=grace_seconds)
        out: list[Candle] = []
        for b in self.builders.values():
            out.extend(b.flush(now))
        return out

    def forming(self, tf: str) -> Candle | None:
        b = self.builders.get(tf)
        return b.current if b else None


class CandleSeries:
    """Closed candles for one instrument/timeframe, bounded in memory."""

    def __init__(self, maxlen: int = 5000):
        self._c: deque[Candle] = deque(maxlen=maxlen)

    def append(self, c: Candle) -> None:
        if self._c and c.ts <= self._c[-1].ts:
            return  # never rewrite history
        self._c.append(c)

    def __len__(self) -> int:
        return len(self._c)

    def __getitem__(self, i):
        return self._c[i]

    def last(self, n: int | None = None) -> list[Candle]:
        if n is None or n >= len(self._c):
            return list(self._c)
        return list(self._c)[-n:]

    def to_frame(self, n: int | None = None) -> pd.DataFrame:
        rows = [c.as_dict() for c in self.last(n)]
        if not rows:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "oi"])
        return pd.DataFrame(rows).drop(columns=["timeframe"])


class CandleStore:
    def __init__(self, maxlen: int = 5000):
        self.maxlen = maxlen
        self._series: dict[tuple[str, str], CandleSeries] = {}

    def series(self, instrument: str, tf: str) -> CandleSeries:
        key = (instrument, tf)
        s = self._series.get(key)
        if s is None:
            s = self._series[key] = CandleSeries(self.maxlen)
        return s

    def add(self, instrument: str, c: Candle) -> None:
        self.series(instrument, c.timeframe).append(c)
