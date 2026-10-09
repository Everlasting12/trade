"""Indicator implementations. Conventions follow TradingView/TA-Lib where they differ:
EMA seeded with SMA, RSI/ATR/ADX use Wilder smoothing, Bollinger uses population stdev."""

from __future__ import annotations

import statistics
from collections import deque

from app.core.models import Candle
from app.indicators.base import (SOURCES, EmaValue, Indicator, RollingMean, WilderValue, true_range)


class SMA(Indicator):
    name = "sma"

    def __init__(self, period: int = 20, source: str = "close"):
        super().__init__(period=period, source=source)
        self._src = SOURCES[source]
        self._m = RollingMean(period)

    def update(self, c: Candle):
        self.value = self._m.push(self._src(c))
        return self.value


class EMA(Indicator):
    name = "ema"

    def __init__(self, period: int = 20, source: str = "close"):
        super().__init__(period=period, source=source)
        self._src = SOURCES[source]
        self._e = EmaValue(period)

    def update(self, c: Candle):
        self.value = self._e.push(self._src(c))
        return self.value


class WMA(Indicator):
    name = "wma"

    def __init__(self, period: int = 20, source: str = "close"):
        super().__init__(period=period, source=source)
        self._src = SOURCES[source]
        self._buf: deque[float] = deque(maxlen=period)
        self._denom = period * (period + 1) / 2

    def update(self, c: Candle):
        self._buf.append(self._src(c))
        if len(self._buf) < self._buf.maxlen:
            self.value = None
        else:
            self.value = sum(w * x for w, x in enumerate(self._buf, start=1)) / self._denom
        return self.value


class RSI(Indicator):
    name = "rsi"

    def __init__(self, period: int = 14, source: str = "close"):
        super().__init__(period=period, source=source)
        self._src = SOURCES[source]
        self._prev: float | None = None
        self._g = WilderValue(period)
        self._l = WilderValue(period)

    def update(self, c: Candle):
        x = self._src(c)
        if self._prev is None:
            self._prev = x
            return None
        ch = x - self._prev
        self._prev = x
        g, l = self._g.push(max(ch, 0.0)), self._l.push(max(-ch, 0.0))
        if g is None or l is None:
            self.value = None
        elif l == 0:
            self.value = 100.0 if g > 0 else 50.0
        else:
            self.value = 100.0 - 100.0 / (1.0 + g / l)
        return self.value


class MACD(Indicator):
    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9, source: str = "close"):
        super().__init__(fast=fast, slow=slow, signal=signal, source=source)
        if fast >= slow:
            raise ValueError("fast must be < slow")
        self._src = SOURCES[source]
        self._f, self._s, self._sig = EmaValue(fast), EmaValue(slow), EmaValue(signal)

    def update(self, c: Candle):
        x = self._src(c)
        f, s = self._f.push(x), self._s.push(x)
        if f is None or s is None:
            return None
        macd = f - s
        sig = self._sig.push(macd)
        self.value = None if sig is None else {"macd": macd, "signal": sig, "hist": macd - sig}
        return self.value


class BollingerBands(Indicator):
    name = "bbands"

    def __init__(self, period: int = 20, mult: float = 2.0, source: str = "close"):
        super().__init__(period=period, mult=mult, source=source)
        self._src = SOURCES[source]
        self._buf: deque[float] = deque(maxlen=period)
        self.mult = mult

    def update(self, c: Candle):
        x = self._src(c)
        self._buf.append(x)
        if len(self._buf) < self._buf.maxlen:
            return None
        mid = statistics.fmean(self._buf)
        sd = statistics.pstdev(self._buf, mid)
        up, lo = mid + self.mult * sd, mid - self.mult * sd
        self.value = {"upper": up, "middle": mid, "lower": lo,
                      "bandwidth": (up - lo) / mid if mid else 0.0,
                      "percent_b": (x - lo) / (up - lo) if up != lo else 0.5}
        return self.value


class ATR(Indicator):
    name = "atr"

    def __init__(self, period: int = 14):
        super().__init__(period=period)
        self._prev_close: float | None = None
        self._w = WilderValue(period)

    def update(self, c: Candle):
        tr = true_range(c, self._prev_close)
        self._prev_close = c.close
        self.value = self._w.push(tr)
        return self.value


class ADX(Indicator):
    """Wilder's ADX with +DI/-DI."""
    name = "adx"

    def __init__(self, period: int = 14):
        super().__init__(period=period)
        self.n = period
        self._prev: Candle | None = None
        self._count = 0
        self._tr = self._pdm = self._mdm = 0.0
        self._adx = WilderValue(period)

    def update(self, c: Candle):
        prev, self._prev = self._prev, c
        if prev is None:
            return None
        up, dn = c.high - prev.high, prev.low - c.low
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        tr = true_range(c, prev.close)
        n = self.n
        if self._count < n:
            self._tr += tr
            self._pdm += pdm
            self._mdm += mdm
            self._count += 1
            if self._count < n:
                return None
        else:
            self._tr = self._tr - self._tr / n + tr
            self._pdm = self._pdm - self._pdm / n + pdm
            self._mdm = self._mdm - self._mdm / n + mdm
        pdi = 100 * self._pdm / self._tr if self._tr else 0.0
        mdi = 100 * self._mdm / self._tr if self._tr else 0.0
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0
        adx = self._adx.push(dx)
        self.value = None if adx is None else {"adx": adx, "plus_di": pdi, "minus_di": mdi}
        return self.value


class Supertrend(Indicator):
    """direction: +1 = uptrend (line below price), -1 = downtrend."""
    name = "supertrend"

    def __init__(self, period: int = 10, multiplier: float = 3.0):
        super().__init__(period=period, multiplier=multiplier)
        self._atr = ATR(period)
        self.m = multiplier
        self._ub: float | None = None
        self._lb: float | None = None
        self._dir: int | None = None
        self._prev_close: float | None = None

    def update(self, c: Candle):
        atr = self._atr.update(c)
        prev_close, self._prev_close = self._prev_close, c.close
        if atr is None:
            return None
        hl2 = (c.high + c.low) / 2
        bub, blb = hl2 + self.m * atr, hl2 - self.m * atr
        ub = bub if (self._ub is None or bub < self._ub or (prev_close is not None and prev_close > self._ub)) else self._ub
        lb = blb if (self._lb is None or blb > self._lb or (prev_close is not None and prev_close < self._lb)) else self._lb
        if self._dir is None or self._dir == -1:
            d = 1 if c.close > ub else -1
        else:
            d = -1 if c.close < lb else 1
        self._ub, self._lb, self._dir = ub, lb, d
        self.value = {"value": lb if d == 1 else ub, "direction": d}
        return self.value


class VWAP(Indicator):
    """Session-anchored VWAP; resets at each new trading day. None when volume is absent (indices)."""
    name = "vwap"

    def __init__(self, source: str = "hlc3"):
        super().__init__(source=source)
        self._src = SOURCES[source]
        self._day = None
        self._pv = self._v = 0.0

    def update(self, c: Candle):
        d = c.ts.date()
        if d != self._day:
            self._day, self._pv, self._v = d, 0.0, 0.0
        self._pv += self._src(c) * c.volume
        self._v += c.volume
        self.value = self._pv / self._v if self._v > 0 else None
        return self.value


class Stochastic(Indicator):
    name = "stoch"

    def __init__(self, k: int = 14, d: int = 3, smooth: int = 3):
        super().__init__(k=k, d=d, smooth=smooth)
        self._h: deque[float] = deque(maxlen=k)
        self._l: deque[float] = deque(maxlen=k)
        self._smooth = RollingMean(smooth)
        self._d = RollingMean(d)

    def update(self, c: Candle):
        self._h.append(c.high)
        self._l.append(c.low)
        if len(self._h) < self._h.maxlen:
            return None
        hh, ll = max(self._h), min(self._l)
        raw = 100 * (c.close - ll) / (hh - ll) if hh != ll else 50.0
        k = self._smooth.push(raw)
        if k is None:
            return None
        d = self._d.push(k)
        self.value = None if d is None else {"k": k, "d": d}
        return self.value


class CCI(Indicator):
    name = "cci"

    def __init__(self, period: int = 20):
        super().__init__(period=period)
        self._buf: deque[float] = deque(maxlen=period)

    def update(self, c: Candle):
        tp = (c.high + c.low + c.close) / 3
        self._buf.append(tp)
        if len(self._buf) < self._buf.maxlen:
            return None
        m = statistics.fmean(self._buf)
        md = statistics.fmean(abs(x - m) for x in self._buf)
        self.value = (tp - m) / (0.015 * md) if md else 0.0
        return self.value


class OBV(Indicator):
    name = "obv"

    def __init__(self):
        super().__init__()
        self._prev: float | None = None
        self._obv = 0.0

    def update(self, c: Candle):
        if self._prev is not None:
            if c.close > self._prev:
                self._obv += c.volume
            elif c.close < self._prev:
                self._obv -= c.volume
        self._prev = c.close
        self.value = self._obv
        return self.value


class VolumeRatio(Indicator):
    """Current volume / SMA(volume, period). >1.5 typically indicates a volume surge."""
    name = "volume_ratio"

    def __init__(self, period: int = 20):
        super().__init__(period=period)
        self._m = RollingMean(period)

    def update(self, c: Candle):
        avg = self._m.push(c.volume)
        self.value = (c.volume / avg) if avg else None
        return self.value


REGISTRY: dict[str, type[Indicator]] = {cls.name: cls for cls in (
    SMA, EMA, WMA, RSI, MACD, BollingerBands, ATR, ADX, Supertrend, VWAP, Stochastic, CCI, OBV, VolumeRatio,
)}


def create(name: str, **params) -> Indicator:
    try:
        cls = REGISTRY[name.lower()]
    except KeyError:
        raise ValueError(f"Unknown indicator {name!r}. Available: {sorted(REGISTRY)}") from None
    return cls(**params)
