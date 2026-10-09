"""Black-Scholes pricing, implied volatility and greeks for European index/stock options.

Kite does not provide IV or greeks, so they are computed locally. Inputs: spot (use the future price
with r=0 for a cleaner forward, or the index with the risk-free rate), strike, time to expiry in years.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime

from app.core.clock import IST, SESSION_CLOSE


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def year_fraction(now: datetime, expiry: date) -> float:
    exp_dt = datetime.combine(expiry, SESSION_CLOSE, IST)
    return max((exp_dt - now).total_seconds(), 0.0) / (365.0 * 86400)


def bs_price(spot: float, strike: float, t: float, vol: float, r: float = 0.0, is_call: bool = True) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + 0.5 * vol * vol) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    if is_call:
        return spot * _ncdf(d1) - strike * math.exp(-r * t) * _ncdf(d2)
    return strike * math.exp(-r * t) * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float, r: float = 0.0, is_call: bool = True,
                lo: float = 1e-4, hi: float = 5.0, tol: float = 1e-6) -> float | None:
    """Bisection (robust; price is monotonic in vol). None if the price is outside no-arbitrage bounds."""
    if price <= 0 or t <= 0 or spot <= 0:
        return None
    intrinsic = max(0.0, spot - strike * math.exp(-r * t)) if is_call else max(0.0, strike * math.exp(-r * t) - spot)
    if price < intrinsic - 1e-9 or price >= (spot if is_call else strike):
        return None
    f_lo = bs_price(spot, strike, t, lo, r, is_call) - price
    f_hi = bs_price(spot, strike, t, hi, r, is_call) - price
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        f_mid = bs_price(spot, strike, t, mid, r, is_call) - price
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return 0.5 * (lo + hi)


@dataclass
class Greeks:
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None     # per calendar day
    vega: float | None      # per 1 vol point

    def as_dict(self) -> dict:
        return {k: (round(v, 6) if v is not None else None) for k, v in asdict(self).items()}


def greeks(price: float, spot: float, strike: float, t: float, r: float = 0.0, is_call: bool = True) -> Greeks:
    iv = implied_vol(price, spot, strike, t, r, is_call)
    if iv is None or t <= 0:
        return Greeks(iv, None, None, None, None)
    st = math.sqrt(t)
    d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * st)
    d2 = d1 - iv * st
    pdf = _npdf(d1)
    delta = _ncdf(d1) if is_call else _ncdf(d1) - 1
    gamma = pdf / (spot * iv * st)
    if is_call:
        theta = -spot * pdf * iv / (2 * st) - r * strike * math.exp(-r * t) * _ncdf(d2)
    else:
        theta = -spot * pdf * iv / (2 * st) + r * strike * math.exp(-r * t) * _ncdf(-d2)
    vega = spot * pdf * st
    return Greeks(iv, delta, gamma, theta / 365, vega / 100)
