"""Instrument master: lookup by key/token and symbolic resolution of rolling contracts.

Never hardcode lot sizes, strikes or expiry weekdays: exchanges change them. Everything comes from
the daily Kite instrument dump.

Symbolic specs accepted by `resolve()`:
    "NSE:INFY"            exact key
    "FUT:NIFTY"           nearest-expiry NIFTY future (NFO; BFO for SENSEX/BANKEX)
    "FUT:BANKNIFTY:next"  next-month future   (near | next | far)
    "INDEX:NIFTY 50"      NSE index (for analysis only, not tradable)
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from app.core.models import Instrument

BSE_UNDERLYINGS = {"SENSEX", "BANKEX", "SENSEX50"}


class InstrumentRegistry:
    def __init__(self, instruments: Iterable[Instrument] = ()):
        self._by_key: dict[str, Instrument] = {}
        self._by_token: dict[int, Instrument] = {}
        self._derivs: dict[str, list[Instrument]] = {}   # underlying name -> FUT/CE/PE
        self.loaded_on: date | None = None
        for i in instruments:
            self.add(i)

    def add(self, inst: Instrument) -> None:
        self._by_key[inst.key] = inst
        self._by_token[inst.token] = inst
        if inst.is_derivative and inst.name:
            self._derivs.setdefault(inst.name, []).append(inst)

    def load_kite_dump(self, rows: Iterable[dict], on: date | None = None) -> int:
        n = 0
        for r in rows:
            try:
                self.add(Instrument.from_kite(r))
                n += 1
            except (KeyError, ValueError, TypeError):
                continue
        self.loaded_on = on or date.today()
        return n

    def __len__(self) -> int:
        return len(self._by_key)

    def __contains__(self, key: str) -> bool:
        return key in self._by_key

    def get(self, key: str) -> Instrument:
        return self._by_key[key]

    def by_token(self, token: int) -> Instrument | None:
        return self._by_token.get(token)

    def search(self, query: str, exchange: str | None = None, limit: int = 25,
               types: tuple[str, ...] | None = None) -> list[Instrument]:
        """Exact symbol, then symbol prefix (nearest expiry first), then name matches.
        `types` filters by instrument_type (EQ, INDEX, FUT, CE, PE) before ranking."""
        q = query.strip().upper()
        if not q:
            return []
        pool = [i for i in self._by_key.values() if (exchange is None or i.exchange == exchange)
                and (types is None or i.instrument_type in types)]
        exact = [i for i in pool if i.tradingsymbol == q]
        prefix = [i for i in pool if i.tradingsymbol.startswith(q) and i.tradingsymbol != q]
        named = [i for i in pool if q in i.name.upper() and not i.tradingsymbol.startswith(q)]
        prefix.sort(key=lambda i: (i.expiry or date.min, i.tradingsymbol))
        return (exact + prefix + named)[:limit]

    # ---------------------------------------------------------------- derivatives
    def futures(self, underlying: str, today: date | None = None) -> list[Instrument]:
        today = today or date.today()
        return sorted((i for i in self._derivs.get(underlying.upper(), []) if i.is_future and i.expiry and i.expiry >= today),
                      key=lambda i: i.expiry)

    def options(self, underlying: str, expiry: date | None = None, today: date | None = None) -> list[Instrument]:
        today = today or date.today()
        return [i for i in self._derivs.get(underlying.upper(), [])
                if i.is_option and i.expiry and i.expiry >= today and (expiry is None or i.expiry == expiry)]

    def option_expiries(self, underlying: str, today: date | None = None) -> list[date]:
        return sorted({i.expiry for i in self.options(underlying, today=today)})

    def underlyings(self) -> list[str]:
        return sorted(self._derivs)

    def resolve(self, spec: str, today: date | None = None) -> Instrument:
        parts = spec.split(":")
        kind = parts[0].upper()
        if kind == "FUT":
            which = parts[2].lower() if len(parts) > 2 else "near"
            futs = self.futures(parts[1], today)
            idx = {"near": 0, "next": 1, "far": 2}[which]
            if len(futs) <= idx:
                raise KeyError(f"No {which} future for {parts[1]}")
            return futs[idx]
        if kind == "INDEX":
            exch = "BSE" if parts[1].upper() in BSE_UNDERLYINGS else "NSE"
            return self.get(f"{exch}:{parts[1]}")
        return self.get(spec)
