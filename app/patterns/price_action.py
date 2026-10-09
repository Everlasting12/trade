"""Price-action analysis: swings, market structure, support/resistance and breakout-style events.

No look-ahead: a swing at index i needs `right` later candles to be confirmed, so it only becomes
visible once candle i+right has closed. All functions take the candles available *now*.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Sequence

from app.core.models import Candle
from app.indicators.library import ATR
from app.patterns.base import PatternResult, avg_volume, body, clamp, rng


@dataclass(slots=True)
class Swing:
    index: int
    price: float
    kind: str            # "high" | "low"
    label: str = ""      # HH / LH / HL / LL (relative to the previous swing of the same kind)


@dataclass(slots=True)
class Level:
    price: float
    kind: str            # "support" | "resistance" (relative to the latest close)
    touches: int
    last_index: int


@dataclass
class PriceActionSnapshot:
    timeframe: str
    trend: str                                   # "up" | "down" | "range"
    atr: float | None
    swings: list[Swing] = field(default_factory=list)
    levels: list[Level] = field(default_factory=list)
    nearest_support: float | None = None
    nearest_resistance: float | None = None
    events: list[PatternResult] = field(default_factory=list)

    def has(self, pattern: str, direction: str | None = None) -> bool:
        return any(e.pattern == pattern and (direction is None or e.direction == direction) for e in self.events)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["swings"] = d["swings"][-8:]
        return d


def find_swings(candles: Sequence[Candle], left: int = 3, right: int = 3) -> list[Swing]:
    n = len(candles)
    swings: list[Swing] = []
    for i in range(left, n - right):
        h, l = candles[i].high, candles[i].low
        win = candles[i - left:i + right + 1]
        if h >= max(c.high for c in win) and all(c.high < h for c in candles[i - left:i]):
            swings.append(Swing(i, h, "high"))
        if l <= min(c.low for c in win) and all(c.low > l for c in candles[i - left:i]):
            swings.append(Swing(i, l, "low"))
    last = {"high": None, "low": None}
    for s in swings:
        prev = last[s.kind]
        if prev is not None:
            if s.kind == "high":
                s.label = "HH" if s.price > prev.price else "LH"
            else:
                s.label = "HL" if s.price > prev.price else "LL"
        last[s.kind] = s
    return swings


def structure_trend(swings: Sequence[Swing]) -> str:
    highs = [s for s in swings if s.kind == "high" and s.label]
    lows = [s for s in swings if s.kind == "low" and s.label]
    if not highs or not lows:
        return "range"
    h, l = highs[-1].label, lows[-1].label
    if h == "HH" and l == "HL":
        return "up"
    if h == "LH" and l == "LL":
        return "down"
    return "range"


def sr_levels(swings: Sequence[Swing], close: float, tolerance: float) -> list[Level]:
    """Cluster swing prices within `tolerance` into levels; more touches = stronger level."""
    clusters: list[list[Swing]] = []
    for s in sorted(swings, key=lambda s: s.price):
        if clusters and abs(s.price - clusters[-1][-1].price) <= tolerance:
            clusters[-1].append(s)
        else:
            clusters.append([s])
    levels = []
    for cl in clusters:
        price = sum(s.price for s in cl) / len(cl)
        levels.append(Level(round(price, 2), "resistance" if price > close else "support", len(cl),
                            max(s.index for s in cl)))
    return levels


def analyze(candles: Sequence[Candle], timeframe: str = "", lookback: int = 200, swing_strength: int = 3,
            consolidation_bars: int = 12, consolidation_atr: float = 2.5) -> PriceActionSnapshot:
    candles = list(candles[-lookback:])
    n = len(candles)
    tf = timeframe or (candles[-1].timeframe if candles else "")
    if n < 2 * swing_strength + 3:
        return PriceActionSnapshot(tf, "range", None)

    atr_vals = ATR.compute(candles, period=14)
    atr = atr_vals[-1] or (sum(rng(c) for c in candles[-14:]) / min(14, n))
    swings = find_swings(candles, swing_strength, swing_strength)
    trend = structure_trend(swings)
    last, prev = candles[-1], candles[-2]
    tol = 0.5 * atr
    # Levels are built from swings confirmed before the latest candle (no use of the current bar).
    past_swings = [s for s in swings if s.index + swing_strength < n - 1]
    levels = sr_levels(past_swings, prev.close, tol)
    res = sorted([lv for lv in levels if lv.price > prev.close], key=lambda lv: lv.price)
    sup = sorted([lv for lv in levels if lv.price < prev.close], key=lambda lv: -lv.price)
    snap = PriceActionSnapshot(tf, trend, atr, swings, levels,
                               sup[0].price if sup else None, res[0].price if res else None)
    av = avg_volume(candles, n - 1)
    vol_ratio = last.volume / av if av else None
    i = n - 1

    def ev(name, direction, conf, confirm=True, **details):
        if vol_ratio is not None:
            conf += 0.1 if vol_ratio >= 1.5 else (-0.1 if vol_ratio < 0.8 else 0)
        snap.events.append(PatternResult(name, direction, round(clamp(conf), 3), tf, i, confirm, not confirm,
                                         {**details, "volume_ratio": round(vol_ratio, 2) if vol_ratio else None}))

    # Breakout / breakdown of the nearest level.
    if res and prev.close <= res[0].price < last.close:
        strength = (last.close - res[0].price) / atr
        ev("breakout", "bullish", 0.5 + min(strength, 1) * 0.2 + 0.05 * min(res[0].touches - 1, 3) +
           (0.1 if trend == "up" else 0), confirm=False, level=res[0].price, touches=res[0].touches)
    if sup and prev.close >= sup[0].price > last.close:
        strength = (sup[0].price - last.close) / atr
        ev("breakdown", "bearish", 0.5 + min(strength, 1) * 0.2 + 0.05 * min(sup[0].touches - 1, 3) +
           (0.1 if trend == "down" else 0), confirm=False, level=sup[0].price, touches=sup[0].touches)

    # Retest: a level broken within the last 10 bars is touched and holds.
    for lv in levels:
        recent = candles[max(0, n - 11):n - 1]
        broke_up = any(a.close <= lv.price < b.close for a, b in zip(recent, recent[1:]))
        broke_dn = any(a.close >= lv.price > b.close for a, b in zip(recent, recent[1:]))
        if broke_up and last.low <= lv.price + tol and last.close > lv.price:
            ev("retest", "bullish", 0.55, level=lv.price)
        elif broke_dn and last.high >= lv.price - tol and last.close < lv.price:
            ev("retest", "bearish", 0.55, level=lv.price)

    # Consolidation and range breakout.
    k = consolidation_bars
    if n > k + 1:
        box = candles[-k - 1:-1]
        hi, lo = max(c.high for c in box), min(c.low for c in box)
        if hi - lo <= consolidation_atr * atr:
            if last.close > hi:
                ev("range_breakout", "bullish", 0.6, confirm=False, range_high=hi, range_low=lo)
            elif last.close < lo:
                ev("range_breakout", "bearish", 0.6, confirm=False, range_high=hi, range_low=lo)
            else:
                ev("consolidation", "neutral", 0.6, range_high=hi, range_low=lo)

    # Momentum breakout: wide-range candle closing near its extreme on high volume.
    b, r = body(last), rng(last)
    if r > 0 and b >= 1.5 * atr and (vol_ratio is None or vol_ratio >= 1.5):
        if last.close > last.open and (last.high - last.close) <= 0.25 * r:
            ev("momentum_breakout", "bullish", 0.6, confirm=False, body_atr=round(b / atr, 2))
        elif last.close < last.open and (last.close - last.low) <= 0.25 * r:
            ev("momentum_breakout", "bearish", 0.6, confirm=False, body_atr=round(b / atr, 2))

    # Trend reversal (change of character): uptrend loses its last higher low, or vice versa.
    lows = [s for s in past_swings if s.kind == "low"]
    highs = [s for s in past_swings if s.kind == "high"]
    if trend == "up" and lows and prev.close >= lows[-1].price > last.close:
        ev("trend_reversal", "bearish", 0.55, level=lows[-1].price)
    elif trend == "down" and highs and prev.close <= highs[-1].price < last.close:
        ev("trend_reversal", "bullish", 0.55, level=highs[-1].price)

    # Latest market-structure labels as informational events.
    for s in swings[-2:]:
        if s.label:
            snap.events.append(PatternResult(
                {"HH": "higher_high", "HL": "higher_low", "LH": "lower_high", "LL": "lower_low"}[s.label],
                "bullish" if s.label in ("HH", "HL") else "bearish", 0.5, tf, s.index, False, True,
                {"price": s.price}))
    return snap
