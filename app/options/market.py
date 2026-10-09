"""Option market data: chain snapshots with IV/greeks, from Kite quotes (paper/live) or a pricing model
(synthetic sessions and backtests).

The forward price used for IV/greeks is implied from put-call parity at the strike where |C - P| is
smallest (F = K + C - P). That works for weekly and monthly expiries alike, without assuming a
dividend yield or carry. Black-76 with r=0 is then used (forward-based Black-Scholes).
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from app.core.clock import IST, SESSION_CLOSE, Clock
from app.core.models import Candle, Instrument, Tick
from app.fno.greeks import bs_price, greeks, year_fraction
from app.market_data.instruments import InstrumentRegistry


@dataclass
class OptionQuote:
    instrument: Instrument
    ltp: float
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    oi: float | None = None
    volume: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None

    @property
    def mid(self) -> float:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return self.ltp

    @property
    def spread_pct(self) -> float | None:
        if self.bid and self.ask and self.mid:
            return 100 * (self.ask - self.bid) / self.mid
        return None

    @property
    def strike(self) -> float:
        return self.instrument.strike or 0.0

    @property
    def kind(self) -> str:
        return self.instrument.instrument_type

    def as_dict(self) -> dict:
        i = self.instrument
        return {"instrument": i.key, "strike": i.strike, "type": i.instrument_type, "expiry": i.expiry,
                "lot_size": i.lot_size, "ltp": self.ltp, "bid": self.bid, "ask": self.ask, "oi": self.oi,
                "volume": self.volume, "iv": self.iv, "delta": self.delta, "gamma": self.gamma, "theta": self.theta,
                "vega": self.vega, "spread_pct": self.spread_pct}


@dataclass
class ChainSnapshot:
    underlying: str
    expiry: date
    forward: float
    ts: datetime
    quotes: list[OptionQuote] = field(default_factory=list)
    source: str = ""

    def of_type(self, kind: str) -> list[OptionQuote]:
        return [q for q in self.quotes if q.kind == kind]

    @property
    def strikes(self) -> list[float]:
        return sorted({q.strike for q in self.quotes})

    @property
    def atm_strike(self) -> float | None:
        ks = self.strikes
        return min(ks, key=lambda k: abs(k - self.forward)) if ks else None

    @property
    def atm_iv(self) -> float | None:
        atm = self.atm_strike
        ivs = [q.iv for q in self.quotes if q.strike == atm and q.iv]
        return sum(ivs) / len(ivs) if ivs else None

    def days_to_expiry(self) -> float:
        exp = datetime.combine(self.expiry, SESSION_CLOSE, IST)
        return max((exp - self.ts).total_seconds() / 86400, 0.0)


def implied_forward(quotes: list[OptionQuote], fallback: float) -> float:
    by_k: dict[float, dict[str, float]] = {}
    for q in quotes:
        if q.mid and q.mid > 0:
            by_k.setdefault(q.strike, {})[q.kind] = q.mid
    pairs = [(k, v["CE"], v["PE"]) for k, v in by_k.items() if "CE" in v and "PE" in v]
    if not pairs:
        return fallback
    k, c, p = min(pairs, key=lambda x: abs(x[1] - x[2]))
    f = k + c - p
    return f if f > 0 else fallback


def add_greeks(quotes: list[OptionQuote], forward: float, now: datetime) -> None:
    for q in quotes:
        t = year_fraction(now, q.instrument.expiry)
        px = q.mid
        if not px or px <= 0 or t <= 0:
            continue
        g = greeks(px, forward, q.strike, t, 0.0, q.kind == "CE")
        q.iv, q.delta, q.gamma, q.theta, q.vega = g.iv, g.delta, g.gamma, g.theta, g.vega


class OptionMarket(ABC):
    is_model = False

    @abstractmethod
    def expiries(self, underlying: str, today: date) -> list[date]: ...

    @abstractmethod
    def chain(self, underlying: str, expiry: date, around: float, width: int = 15) -> ChainSnapshot: ...


class KiteOptionMarket(OptionMarket):
    """Real chain from Kite quotes. Cached briefly (quote API is rate-limited)."""

    def __init__(self, kite: Any, registry: InstrumentRegistry, clock: Clock, cache_seconds: float = 3.0):
        self.kite = kite
        self.registry = registry
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._cache: dict[tuple, tuple[float, ChainSnapshot]] = {}

    def expiries(self, underlying: str, today: date) -> list[date]:
        return self.registry.option_expiries(underlying, today)

    def chain(self, underlying: str, expiry: date, around: float, width: int = 15) -> ChainSnapshot:
        key = (underlying, expiry, round(around, -1), width)
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_seconds:
            return hit[1]
        from app.fno.chain import chain_instruments
        contracts = chain_instruments(self.registry, underlying, expiry, around, width=width, today=self.clock.now().date())
        quotes_raw: dict = {}
        keys = [c.key for c in contracts]
        for i in range(0, len(keys), 450):
            quotes_raw.update(self.kite.quote(keys[i:i + 450]))
        quotes = []
        for c in contracts:
            q = quotes_raw.get(c.key) or {}
            depth = q.get("depth") or {}
            b = (depth.get("buy") or [{}])[0]
            s = (depth.get("sell") or [{}])[0]
            quotes.append(OptionQuote(c, float(q.get("last_price") or 0), b.get("price") or None, s.get("price") or None,
                                      b.get("quantity") or None, s.get("quantity") or None, q.get("oi"), q.get("volume")))
        now = self.clock.now()
        fwd = implied_forward(quotes, around)
        add_greeks(quotes, fwd, now)
        snap = ChainSnapshot(underlying, expiry, fwd, now, quotes, "kite")
        self._cache[key] = (time.monotonic(), snap)
        return snap


class ModelOptionMarket(OptionMarket):
    """Black-76 priced option chain on top of an underlying price (synthetic sessions and backtests).

    Clearly a model: flat-ish IV with a simple smile, fixed spread, weekly expiries. Good for testing
    the decision/execution pipeline; NOT a substitute for real option data when judging profitability.
    """

    is_model = True

    def __init__(self, registry: InstrumentRegistry, price_of: Callable[[str], float | None], clock: Clock,
                 iv: float = 0.14, smile: float = 2.0, spread_pct: float = 0.6, expiry_weekday: int = 1,
                 strike_steps: dict[str, float] | None = None, depth_qty: int = 5000, token_base: int = 7_000_000):
        self.registry = registry
        self.price_of = price_of
        self.clock = clock
        self.iv = iv
        self.smile = smile
        self.spread_pct = spread_pct
        self.expiry_weekday = expiry_weekday
        self.strike_steps = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "SENSEX": 100,
                             **(strike_steps or {})}
        self.depth_qty = depth_qty
        self._next_token = token_base
        self._made: dict[tuple, Instrument] = {}

    def step(self, underlying: str, price: float) -> float:
        if underlying in self.strike_steps:
            return self.strike_steps[underlying]
        return 5 if price < 500 else 10 if price < 2000 else 20 if price < 5000 else 50

    def expiries(self, underlying: str, today: date) -> list[date]:
        d = today + timedelta(days=(self.expiry_weekday - today.weekday()) % 7)
        return [d + timedelta(weeks=i) for i in range(4)]

    def _lot(self, underlying: str) -> int:
        futs = self.registry.futures(underlying, date.min)
        return futs[0].lot_size if futs else 1

    def instrument(self, underlying: str, expiry: date, strike: float, kind: str) -> Instrument:
        k = (underlying, expiry, strike, kind)
        inst = self._made.get(k)
        if inst is None:
            self._next_token += 1
            sym = f"{underlying}{expiry:%y%m%d}{int(strike) if strike == int(strike) else strike}{kind}"
            inst = Instrument(self._next_token, "NFO", sym, kind, "NFO-OPT", underlying, self._lot(underlying), 0.05,
                              expiry, float(strike))
            self._made[k] = inst
            self.registry.add(inst)
        return inst

    def vol(self, strike: float, forward: float) -> float:
        m = math.log(strike / forward)
        return self.iv * (1 + self.smile * abs(m))

    def premium(self, inst: Instrument, forward: float, now: datetime) -> float:
        t = year_fraction(now, inst.expiry)
        px = bs_price(forward, inst.strike, t, self.vol(inst.strike, forward), 0.0, inst.instrument_type == "CE")
        return max(round(round(px / 0.05) * 0.05, 2), 0.05)

    def quote(self, inst: Instrument, forward: float, now: datetime) -> OptionQuote:
        mid = self.premium(inst, forward, now)
        half = max(0.05, round(mid * self.spread_pct / 200 / 0.05) * 0.05)
        return OptionQuote(inst, mid, max(0.05, round(mid - half, 2)), round(mid + half, 2), self.depth_qty,
                           self.depth_qty, 100_000.0, 0.0)

    def chain(self, underlying: str, expiry: date, around: float, width: int = 15) -> ChainSnapshot:
        now = self.clock.now()
        fwd = self.price_of(underlying) or around
        step = self.step(underlying, fwd)
        atm = round(fwd / step) * step
        quotes = []
        for i in range(-width, width + 1):
            k = atm + i * step
            if k <= 0:
                continue
            for kind in ("CE", "PE"):
                quotes.append(self.quote(self.instrument(underlying, expiry, float(k), kind), fwd, now))
        add_greeks(quotes, fwd, now)
        return ChainSnapshot(underlying, expiry, fwd, now, quotes, "model")

    # ---- continuous pricing of held contracts (driven by the underlying)
    def model_tick(self, inst: Instrument, underlying_price: float, ts: datetime) -> Tick:
        q = self.quote(inst, underlying_price, ts)
        return Tick(inst.token, ts, q.ltp, None, None, q.bid, q.ask, q.bid_qty, q.ask_qty)

    def model_bar(self, inst: Instrument, ub: Candle, start: datetime, end: datetime) -> Candle:
        """Option OHLC implied by an underlying bar: calls peak with the underlying high, puts with its low."""
        o = self.premium(inst, ub.open, start)
        c = self.premium(inst, ub.close, end)
        at_high = self.premium(inst, ub.high, end)
        at_low = self.premium(inst, ub.low, end)
        hi, lo = max(o, c, at_high, at_low), min(o, c, at_high, at_low)
        return Candle(start, o, hi, lo, c, 0.0, None, ub.timeframe)
