"""Option chain construction and contract selection.

`build_chain` combines the instrument master with a quote snapshot (Kite `quote()` output keyed by
"NFO:SYMBOL") into a strike-wise table with OI, volume, bid/ask, IV and greeks, plus summary
analytics (ATM strike, PCR, max pain). `InstrumentSelector` picks the contract a strategy should
trade: future, or CE/PE by expiry (near/next/monthly) and moneyness (ATM, ITM n, OTM n).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.core.models import Instrument
from app.fno.greeks import greeks, year_fraction
from app.market_data.instruments import InstrumentRegistry


def strikes_for(options: list[Instrument]) -> list[float]:
    return sorted({o.strike for o in options if o.strike is not None})


def atm_strike(strikes: list[float], spot: float) -> float:
    return min(strikes, key=lambda k: (abs(k - spot), k))


def chain_instruments(reg: InstrumentRegistry, underlying: str, expiry: date, spot: float,
                      width: int = 15, today: date | None = None) -> list[Instrument]:
    """CE+PE contracts for `width` strikes either side of ATM (keeps quote() calls under Kite's 500 limit)."""
    opts = reg.options(underlying, expiry, today)
    ks = strikes_for(opts)
    if not ks:
        return []
    atm = ks.index(atm_strike(ks, spot))
    keep = set(ks[max(0, atm - width): atm + width + 1])
    return sorted((o for o in opts if o.strike in keep), key=lambda o: (o.strike, o.instrument_type))


def build_chain(contracts: list[Instrument], quotes: dict[str, dict], spot: float, now: datetime,
                rate: float = 0.0, oi_baseline: dict[int, float] | None = None) -> dict:
    """`oi_baseline`: OI per token at an earlier snapshot (e.g. first fetch of the day) for change-in-OI."""
    rows: dict[float, dict] = {}
    for inst in contracts:
        q = quotes.get(inst.key) or {}
        ltp = q.get("last_price") or 0.0
        depth = q.get("depth") or {}
        bid = ((depth.get("buy") or [{}])[0]).get("price") or None
        ask = ((depth.get("sell") or [{}])[0]).get("price") or None
        mid = (bid + ask) / 2 if bid and ask else ltp
        t = year_fraction(now, inst.expiry)
        g = greeks(mid, spot, inst.strike, t, rate, inst.instrument_type == "CE") if mid and spot else None
        oi = q.get("oi")
        base = (oi_baseline or {}).get(inst.token)
        side = "ce" if inst.instrument_type == "CE" else "pe"
        row = rows.setdefault(inst.strike, {"strike": inst.strike})
        row.update({
            f"{side}_symbol": inst.tradingsymbol, f"{side}_token": inst.token, f"{side}_ltp": ltp,
            f"{side}_bid": bid, f"{side}_ask": ask, f"{side}_volume": q.get("volume"), f"{side}_oi": oi,
            f"{side}_oi_change": (oi - base) if (oi is not None and base is not None) else None,
            f"{side}_iv": round(100 * g.iv, 2) if g and g.iv else None,
            f"{side}_delta": round(g.delta, 3) if g and g.delta is not None else None,
            f"{side}_gamma": round(g.gamma, 6) if g and g.gamma is not None else None,
            f"{side}_theta": round(g.theta, 2) if g and g.theta is not None else None,
            f"{side}_vega": round(g.vega, 2) if g and g.vega is not None else None,
        })
    table = [rows[k] for k in sorted(rows)]
    strikes = [r["strike"] for r in table]
    ce_oi = sum(r.get("ce_oi") or 0 for r in table)
    pe_oi = sum(r.get("pe_oi") or 0 for r in table)
    return {
        "spot": spot,
        "atm_strike": atm_strike(strikes, spot) if strikes else None,
        "pcr_oi": round(pe_oi / ce_oi, 3) if ce_oi else None,
        "max_pain": max_pain(table),
        "total_ce_oi": ce_oi, "total_pe_oi": pe_oi,
        "rows": table,
    }


def max_pain(table: list[dict]) -> float | None:
    """Strike at which total intrinsic value paid to option holders is minimal."""
    strikes = [r["strike"] for r in table]
    if not strikes:
        return None
    best, best_val = None, None
    for s in strikes:
        pain = sum((r.get("ce_oi") or 0) * max(0.0, s - r["strike"]) + (r.get("pe_oi") or 0) * max(0.0, r["strike"] - s)
                   for r in table)
        if best_val is None or pain < best_val:
            best, best_val = s, pain
    return best


@dataclass
class ContractSpec:
    underlying: str
    kind: str = "FUT"            # FUT | CE | PE
    expiry: str = "near"         # near | next | monthly | YYYY-MM-DD
    moneyness: str = "ATM"       # ATM | ITM1..n | OTM1..n


class InstrumentSelector:
    def __init__(self, registry: InstrumentRegistry):
        self.reg = registry

    def _expiry(self, underlying: str, which: str, kind: str, today: date) -> date:
        if kind == "FUT":
            exps = [f.expiry for f in self.reg.futures(underlying, today)]
        else:
            exps = self.reg.option_expiries(underlying, today)
        if not exps:
            raise KeyError(f"No {kind} expiries for {underlying}")
        if which == "near":
            return exps[0]
        if which == "next":
            return exps[1] if len(exps) > 1 else exps[0]
        if which == "monthly":  # last expiry of the nearest month that has one
            m = (exps[0].year, exps[0].month)
            return max(e for e in exps if (e.year, e.month) == m)
        return date.fromisoformat(which)

    def select(self, spec: ContractSpec, spot: float | None = None, today: date | None = None) -> Instrument:
        today = today or date.today()
        exp = self._expiry(spec.underlying, spec.expiry, spec.kind, today)
        if spec.kind == "FUT":
            return next(f for f in self.reg.futures(spec.underlying, today) if f.expiry == exp)
        if spot is None:
            raise ValueError("spot price required to select an option strike")
        opts = [o for o in self.reg.options(spec.underlying, exp, today) if o.instrument_type == spec.kind]
        ks = strikes_for(opts)
        if not ks:
            raise KeyError(f"No {spec.kind} options for {spec.underlying} {exp}")
        i = ks.index(atm_strike(ks, spot))
        m = spec.moneyness.upper()
        n = int(m[3:] or 1) if m != "ATM" else 0
        # For calls ITM = lower strikes; for puts ITM = higher strikes.
        direction = {"CE": {"ITM": -1, "OTM": 1}, "PE": {"ITM": 1, "OTM": -1}}[spec.kind].get(m[:3], 0)
        j = min(max(i + direction * n, 0), len(ks) - 1)
        return next(o for o in opts if o.strike == ks[j])
