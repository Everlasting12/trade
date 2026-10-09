"""Larger chart structures detected from confirmed swing points.

These are heuristics with tolerances scaled by ATR. They report `confirmed=True` only after the
decisive level (neckline / trendline / range) has been broken by a candle close.
"""

from __future__ import annotations

from typing import Sequence

from app.core.models import Candle
from app.indicators.library import ATR
from app.patterns.base import PatternResult, clamp, rng
from app.patterns.price_action import Swing, find_swings


def _slope(points: list[Swing]) -> tuple[float, float]:
    """Least squares (slope per bar, intercept)."""
    n = len(points)
    mx = sum(p.index for p in points) / n
    my = sum(p.price for p in points) / n
    var = sum((p.index - mx) ** 2 for p in points)
    slope = sum((p.index - mx) * (p.price - my) for p in points) / var if var else 0.0
    return slope, my - slope * mx


def detect(candles: Sequence[Candle], timeframe: str = "", lookback: int = 150, swing_strength: int = 3) -> list[PatternResult]:
    candles = list(candles[-lookback:])
    n = len(candles)
    if n < 30:
        return []
    tf = timeframe or candles[-1].timeframe
    atr = ATR.compute(candles, period=14)[-1] or sum(rng(c) for c in candles[-14:]) / 14
    swings = find_swings(candles, swing_strength, swing_strength)
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    last = candles[-1]
    i = n - 1
    out: list[PatternResult] = []
    tol = 0.6 * atr

    def add(name, direction, conf, confirmed, **details):
        out.append(PatternResult(name, direction, round(clamp(conf + (0.15 if confirmed else 0)), 3), tf, i,
                                 True, confirmed, details))

    def between(a: Swing, b: Swing, kind: str) -> list[Swing]:
        return [s for s in swings if s.kind == kind and a.index < s.index < b.index]

    # Double top / bottom: last two swing highs (lows) at about the same price with a meaningful dip between.
    if len(highs) >= 2:
        a, b = highs[-2], highs[-1]
        mid = between(a, b, "low")
        if mid and abs(a.price - b.price) <= tol and max(a.price, b.price) - min(m.price for m in mid) >= 1.5 * atr:
            neck = min(m.price for m in mid)
            add("double_top", "bearish", 0.55, last.close < neck, top=round((a.price + b.price) / 2, 2), neckline=neck)
    if len(lows) >= 2:
        a, b = lows[-2], lows[-1]
        mid = between(a, b, "high")
        if mid and abs(a.price - b.price) <= tol and max(m.price for m in mid) - min(a.price, b.price) >= 1.5 * atr:
            neck = max(m.price for m in mid)
            add("double_bottom", "bullish", 0.55, last.close > neck, bottom=round((a.price + b.price) / 2, 2), neckline=neck)

    # Head & shoulders (and inverse).
    if len(highs) >= 3:
        l, h, r = highs[-3:]
        if h.price - max(l.price, r.price) >= atr and abs(l.price - r.price) <= 1.5 * tol:
            troughs = between(l, r, "low")
            if troughs:
                neck = sum(t.price for t in troughs) / len(troughs)
                add("head_and_shoulders", "bearish", 0.6, last.close < neck, head=h.price, neckline=round(neck, 2))
    if len(lows) >= 3:
        l, h, r = lows[-3:]
        if min(l.price, r.price) - h.price >= atr and abs(l.price - r.price) <= 1.5 * tol:
            peaks = between(l, r, "high")
            if peaks:
                neck = sum(t.price for t in peaks) / len(peaks)
                add("inverse_head_and_shoulders", "bullish", 0.6, last.close > neck, head=h.price, neckline=round(neck, 2))

    # Triangles and channels from the last 3+ swing highs and lows.
    if len(highs) >= 3 and len(lows) >= 3:
        hs, ls = highs[-4:], lows[-4:]
        sh, ih = _slope(hs)
        sl, il = _slope(ls)
        flat = 0.03 * atr  # price change per bar considered "flat"
        upper, lower = sh * i + ih, sl * i + il
        brk_up, brk_dn = last.close > upper, last.close < lower
        details = {"upper": round(upper, 2), "lower": round(lower, 2),
                   "slope_high": round(sh, 4), "slope_low": round(sl, 4)}
        if abs(sh) <= flat and sl > flat:
            add("ascending_triangle", "bullish", 0.55, brk_up, **details)
        elif sh < -flat and abs(sl) <= flat:
            add("descending_triangle", "bearish", 0.55, brk_dn, **details)
        elif sh < -flat and sl > flat:
            add("symmetrical_triangle", "bullish" if brk_up else "bearish" if brk_dn else "neutral", 0.5,
                brk_up or brk_dn, **details)
        elif abs(sh - sl) <= max(flat, 0.25 * max(abs(sh), abs(sl))) and abs(sh) > flat:
            add("channel_up" if sh > 0 else "channel_down", "bullish" if sh > 0 else "bearish", 0.5,
                False, **details)

    # Flag / pennant: strong pole followed by a tight consolidation.
    pole_len, cons_len = 10, 8
    if n >= pole_len + cons_len + 1:
        pole = candles[-(pole_len + cons_len + 1):-(cons_len + 1)]
        cons = candles[-(cons_len + 1):-1]
        move = pole[-1].close - pole[0].open
        c_hi, c_lo = max(c.high for c in cons), min(c.low for c in cons)
        if abs(move) >= 4 * atr and (c_hi - c_lo) <= 0.5 * abs(move):
            first_half, second_half = cons[:cons_len // 2], cons[cons_len // 2:]
            rng1 = max(c.high for c in first_half) - min(c.low for c in first_half)
            rng2 = max(c.high for c in second_half) - min(c.low for c in second_half)
            name = "pennant" if rng2 < 0.7 * rng1 else "flag"
            direction = "bullish" if move > 0 else "bearish"
            confirmed = last.close > c_hi if move > 0 else last.close < c_lo
            add(name, direction, 0.55, confirmed, pole=round(move, 2), range_high=c_hi, range_low=c_lo)

    # Cup & handle (heuristic on closes).
    w = min(n - 1, 80)
    if w >= 40:
        seg = candles[-w - 1:-1]
        closes = [c.close for c in seg]
        third = w // 3
        left_i = max(range(third), key=lambda k: closes[k])
        bottom_i = min(range(third, 2 * third), key=lambda k: closes[k])
        right_i = max(range(2 * third, w - w // 8), key=lambda k: closes[k])
        left, bottom, right = closes[left_i], closes[bottom_i], closes[right_i]
        depth = min(left, right) - bottom
        handle = closes[right_i:]
        if depth >= 3 * atr and abs(left - right) <= 0.15 * depth and handle:
            pullback = right - min(handle)
            if 0 < pullback <= 0.5 * depth:
                rim = max(left, right)
                add("cup_and_handle", "bullish", 0.5, last.close > rim, rim=rim, bottom=bottom)
    return out
