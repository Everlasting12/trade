"""Indicator framework.

Every indicator is *incremental*: `update(candle)` consumes one closed candle and returns the
current value in O(1) or O(period). The batch API (`compute`) simply replays `update`, so live,
paper, backtest and charts all use exactly the same arithmetic.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.core.models import Candle

SOURCES: dict[str, Callable[[Candle], float]] = {
    "open": lambda c: c.open,
    "high": lambda c: c.high,
    "low": lambda c: c.low,
    "close": lambda c: c.close,
    "volume": lambda c: c.volume,
    "oi": lambda c: c.oi or 0.0,
    "hl2": lambda c: (c.high + c.low) / 2,
    "hlc3": lambda c: (c.high + c.low + c.close) / 3,
    "ohlc4": lambda c: (c.open + c.high + c.low + c.close) / 4,
}


class Indicator(ABC):
    name: str = ""

    def __init__(self, **params: Any):
        self.params = params
        self.value: Any = None

    @property
    def ready(self) -> bool:
        return self.value is not None

    @abstractmethod
    def update(self, candle: Candle) -> Any: ...

    @classmethod
    def compute(cls, candles: Iterable[Candle], **params: Any) -> list[Any]:
        ind = cls(**params)
        return [ind.update(c) for c in candles]

    def __repr__(self) -> str:
        args = ",".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({args})"


# ---- scalar building blocks reused by several indicators ----

class RollingMean:
    def __init__(self, n: int):
        if n < 1:
            raise ValueError("period must be >= 1")
        self.n = n
        self.buf: deque[float] = deque(maxlen=n)
        self._sum = 0.0
        self._updates = 0

    def push(self, x: float) -> float | None:
        if len(self.buf) == self.n:
            self._sum -= self.buf[0]
        self.buf.append(x)
        self._sum += x
        self._updates += 1
        if self._updates % 1000 == 0:  # bound floating-point drift
            self._sum = math.fsum(self.buf)
        return self._sum / self.n if len(self.buf) == self.n else None


class EmaValue:
    """EMA seeded with the SMA of the first `n` values (TA-Lib / TradingView convention)."""

    def __init__(self, n: int):
        if n < 1:
            raise ValueError("period must be >= 1")
        self.n = n
        self.alpha = 2.0 / (n + 1)
        self._seed: list[float] = []
        self.value: float | None = None

    def push(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
                self._seed = []
            return self.value
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class WilderValue:
    """Wilder's smoothing (RMA): seed = mean of first n values, then (prev*(n-1)+x)/n."""

    def __init__(self, n: int):
        self.n = n
        self._seed: list[float] = []
        self.value: float | None = None

    def push(self, x: float) -> float | None:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) == self.n:
                self.value = sum(self._seed) / self.n
                self._seed = []
            return self.value
        self.value = (self.value * (self.n - 1) + x) / self.n
        return self.value


def true_range(c: Candle, prev_close: float | None) -> float:
    if prev_close is None:
        return c.high - c.low
    return max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))


@dataclass(frozen=True)
class IndicatorSpec:
    """Identifies an indicator instance: name + params + timeframe. Hashable, usable as a key."""
    name: str
    params: tuple[tuple[str, Any], ...] = ()
    timeframe: str = ""

    @classmethod
    def of(cls, name: str, timeframe: str = "", **params: Any) -> "IndicatorSpec":
        return cls(name.lower(), tuple(sorted(params.items())), timeframe)

    @property
    def kwargs(self) -> dict[str, Any]:
        return dict(self.params)

    @property
    def key(self) -> str:
        args = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.name}({args})@{self.timeframe}"

    def with_timeframe(self, tf: str) -> "IndicatorSpec":
        return IndicatorSpec(self.name, self.params, tf)
