"""Candlestick pattern detection.

Each detector looks at the candle(s) ending at index `i` and returns PatternResult objects.
Confidence = shape quality, adjusted by context (prior trend, volume). A single-candle pattern is
never treated as a signal on its own: `confirmation_required=True` means a strategy should wait for
the next candle to confirm (e.g. close above the hammer's high).
"""

from __future__ import annotations

from typing import Sequence

from app.core.models import Candle
from app.patterns.base import (PatternResult, avg_volume, body, clamp, is_bear, is_bull, lower_wick, prior_trend,
                               rng, upper_wick)


def _context(conf: float, candles: Sequence[Candle], i: int, wants_trend: int) -> tuple[float, dict]:
    trend = prior_trend(candles, i, lookback=10)
    av = avg_volume(candles, i)
    vol_ratio = candles[i].volume / av if av else None
    if wants_trend != 0:
        conf += 0.15 if trend == wants_trend else (-0.2 if trend == -wants_trend else -0.05)
    if vol_ratio is not None:
        conf += 0.1 if vol_ratio >= 1.5 else (-0.05 if vol_ratio < 0.7 else 0.0)
    return clamp(conf), {"prior_trend": trend, "volume_ratio": round(vol_ratio, 2) if vol_ratio else None}


def detect_at(candles: Sequence[Candle], i: int, timeframe: str = "") -> list[PatternResult]:
    if i < 0:
        i += len(candles)
    out: list[PatternResult] = []
    c = candles[i]
    r = rng(c)
    if r <= 0:
        return out
    b, uw, lw = body(c), upper_wick(c), lower_wick(c)
    tf = timeframe or c.timeframe
    trend_before = prior_trend(candles, i, 10)

    def add(name, direction, base_conf, wants_trend, confirm=True, **details):
        conf, ctx = _context(base_conf, candles, i, wants_trend)
        out.append(PatternResult(name, direction, round(conf, 3), tf, i, confirm, False, {**details, **ctx}))

    # --- single candle ---
    if b <= 0.1 * r:
        add("doji", "neutral", 0.5, 0, body_pct=round(b / r, 3))
    small_body = b <= 0.35 * r
    if small_body and lw >= 2 * max(b, 1e-9) and uw <= 0.15 * r:
        if trend_before < 0:
            add("hammer", "bullish", 0.55 + min(lw / r, 1) * 0.2, -1)
        elif trend_before > 0:
            add("hanging_man", "bearish", 0.45 + min(lw / r, 1) * 0.15, 1)
    if small_body and uw >= 2 * max(b, 1e-9) and lw <= 0.15 * r:
        if trend_before > 0:
            add("shooting_star", "bearish", 0.55 + min(uw / r, 1) * 0.2, 1)
        elif trend_before < 0:
            add("inverted_hammer", "bullish", 0.5 + min(uw / r, 1) * 0.15, -1)
    # Pin bar: one dominant wick (>= 2/3 of range) regardless of trend; direction from the wick.
    if lw >= 0.66 * r and b <= 0.3 * r:
        add("pin_bar", "bullish", 0.5, -1, wick_pct=round(lw / r, 3))
    elif uw >= 0.66 * r and b <= 0.3 * r:
        add("pin_bar", "bearish", 0.5, 1, wick_pct=round(uw / r, 3))

    # --- two candles ---
    if i >= 1:
        p = candles[i - 1]
        pb = body(p)
        if pb > 0 and b > 0:
            if is_bear(p) and is_bull(c) and c.open <= p.close and c.close >= p.open and b > pb:
                add("bullish_engulfing", "bullish", 0.6 + clamp(b / pb - 1, 0, 1) * 0.15, -1, confirm=False)
            elif is_bull(p) and is_bear(c) and c.open >= p.close and c.close <= p.open and b > pb:
                add("bearish_engulfing", "bearish", 0.6 + clamp(b / pb - 1, 0, 1) * 0.15, 1, confirm=False)
            elif b < 0.5 * pb and max(c.open, c.close) <= max(p.open, p.close) and min(c.open, c.close) >= min(p.open, p.close):
                if is_bear(p):
                    add("bullish_harami", "bullish", 0.5, -1)
                elif is_bull(p):
                    add("bearish_harami", "bearish", 0.5, 1)

    # --- three candles ---
    if i >= 2:
        a, m = candles[i - 2], candles[i - 1]
        ab = body(a)
        if ab > 0 and body(m) <= 0.4 * ab:
            mid_a = (a.open + a.close) / 2
            if is_bear(a) and is_bull(c) and c.close > mid_a and max(m.open, m.close) <= a.close + 0.1 * ab:
                add("morning_star", "bullish", 0.65, -1, confirm=False)
            elif is_bull(a) and is_bear(c) and c.close < mid_a and min(m.open, m.close) >= a.close - 0.1 * ab:
                add("evening_star", "bearish", 0.65, 1, confirm=False)
    return out


def detect_recent(candles: Sequence[Candle], lookback: int = 3, timeframe: str = "") -> list[PatternResult]:
    """Patterns ending on any of the last `lookback` candles. Marks single-candle patterns as
    `confirmed` if a later candle closed beyond the pattern candle in the pattern's direction."""
    n = len(candles)
    out = []
    for i in range(max(0, n - lookback), n):
        for p in detect_at(candles, i, timeframe):
            if p.confirmation_required and i < n - 1:
                nxt = candles[i + 1]
                if (p.direction == "bullish" and nxt.close > candles[i].high) or \
                   (p.direction == "bearish" and nxt.close < candles[i].low):
                    p.confirmed = True
                    p.confidence = round(clamp(p.confidence + 0.1), 3)
            out.append(p)
    return out
