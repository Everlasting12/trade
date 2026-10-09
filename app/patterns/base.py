from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from app.core.models import Candle


@dataclass(slots=True)
class PatternResult:
    pattern: str
    direction: str                       # "bullish" | "bearish" | "neutral"
    confidence: float                    # 0..1, heuristic quality score - NOT a probability of profit
    timeframe: str
    candle_index: int                    # index in the analysed sequence of the last candle of the pattern
    confirmation_required: bool = True
    confirmed: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def body(c: Candle) -> float:
    return abs(c.close - c.open)


def rng(c: Candle) -> float:
    return c.high - c.low


def upper_wick(c: Candle) -> float:
    return c.high - max(c.open, c.close)


def lower_wick(c: Candle) -> float:
    return min(c.open, c.close) - c.low


def is_bull(c: Candle) -> bool:
    return c.close > c.open


def is_bear(c: Candle) -> bool:
    return c.close < c.open


def prior_trend(candles: Sequence[Candle], end: int, lookback: int = 10) -> int:
    """+1 / -1 / 0 trend of closes *before* index `end` (exclusive), via the slope of a least-squares
    fit normalised by price. Only uses data available at `end`."""
    start = max(0, end - lookback)
    xs = [c.close for c in candles[start:end]]
    if len(xs) < 4:
        return 0
    n = len(xs)
    mx, my = (n - 1) / 2, sum(xs) / n
    cov = sum((i - mx) * (x - my) for i, x in enumerate(xs))
    var = sum((i - mx) ** 2 for i in range(n))
    slope_pct = (cov / var) / my * n if my else 0.0  # total % move over the window
    if slope_pct > 0.004:
        return 1
    if slope_pct < -0.004:
        return -1
    return 0


def avg_volume(candles: Sequence[Candle], end: int, lookback: int = 20) -> float:
    xs = [c.volume for c in candles[max(0, end - lookback):end]]
    return sum(xs) / len(xs) if xs else 0.0
