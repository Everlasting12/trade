"""Market data: instrument search, quotes, candles + indicators + patterns, option chain, live watchlist."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.core.clock import IST, now_ist
from app.core.models import Candle
from app.fno.chain import build_chain, chain_instruments
from app.indicators.base import IndicatorSpec
from app.indicators.engine import compute_frame
from app.market_data import timeframes as tfm
from app.market_data.historical.service import HistoricalDataService
from app.patterns import candlestick, chart_patterns, price_action
from backend.deps import container, kite

router = APIRouter(prefix="/market", tags=["market"])

# Positional parameter order for compact indicator strings like "ema:20", "bbands:20,2", "supertrend:10,3".
_POSITIONAL = {
    "sma": ["period"], "ema": ["period"], "wma": ["period"], "rsi": ["period"], "atr": ["period"],
    "adx": ["period"], "cci": ["period"], "volume_ratio": ["period"], "bbands": ["period", "mult"],
    "supertrend": ["period", "multiplier"], "macd": ["fast", "slow", "signal"], "stoch": ["k", "d", "smooth"],
    "vwap": [], "obv": [],
}


def parse_indicator(s: str, tf: str) -> IndicatorSpec:
    name, _, args = s.partition(":")
    name = name.strip().lower()
    if name not in _POSITIONAL:
        raise HTTPException(400, f"Unknown indicator {name!r}. Use one of {sorted(_POSITIONAL)}")
    vals = [a for a in args.split(",") if a.strip()] if args else []
    params = {}
    for key, v in zip(_POSITIONAL[name], vals):
        params[key] = float(v) if "." in v else int(v)
    return IndicatorSpec.of(name, tf, **params)


def _clean(v):
    if v is None:
        return None
    try:
        import math
        return None if (isinstance(v, float) and math.isnan(v)) else v
    except Exception:
        return v


@router.get("/instruments/search")
def search(q: str, exchange: str | None = None, limit: int = 25):
    reg = container().instruments.registry()
    return [{"token": i.token, "key": i.key, "exchange": i.exchange, "tradingsymbol": i.tradingsymbol, "name": i.name,
             "instrument_type": i.instrument_type, "expiry": i.expiry, "strike": i.strike, "lot_size": i.lot_size,
             "tick_size": i.tick_size} for i in reg.search(q, exchange.upper() if exchange else None, limit)]


@router.get("/underlyings")
def underlyings():
    return container().instruments.registry().underlyings()


@router.get("/expiries")
def expiries(underlying: str):
    reg = container().instruments.registry()
    return {"options": reg.option_expiries(underlying), "futures": [f.expiry for f in reg.futures(underlying)]}


@router.get("/quote")
def quote(i: list[str] = Query(...), k=Depends(kite)):
    return k.quote(i)


def _resolve(instrument: str):
    """Engine instruments first (works in synthetic mode without a Kite login), then the Kite master."""
    c = container()
    eng = c.runtime.engine
    if eng is not None:
        try:
            return eng.registry.resolve(instrument)
        except KeyError:
            pass
    try:
        return c.instruments.registry().resolve(instrument)
    except KeyError:
        raise HTTPException(404, f"Unknown instrument {instrument}")


@router.get("/candles")
def candles(instrument: str, tf: str = "5m", days: int = 5, ind: list[str] = Query(default=[]),
            source: str = "auto"):
    """Historical candles (Kite, cached) with indicator columns and pattern analysis of the latest candles.
    source=engine uses the engine's live-built candles instead (no historical add-on needed)."""
    c = container()
    tfm.validate(tf)
    inst = _resolve(instrument)
    rows: list[Candle] = []
    used = source
    if source in ("auto", "kite") and c.session.logged_in:
        try:
            hist = HistoricalDataService(c.session.kite, c.repo)
            now = now_ist()
            rows = hist.fetch(inst.token, tf, now - timedelta(days=days), now, oi=inst.is_derivative)
            used = "kite"
        except Exception as e:
            if source == "kite":
                raise HTTPException(502, f"Historical data unavailable: {e}")
    if source == "kite" and not c.session.logged_in:
        raise HTTPException(401, "Log in with Kite for historical data")
    if not rows and c.runtime.status == "running" and inst.key in (c.runtime.engine.aggregators if c.runtime.engine else {}):
        rows = [Candle(datetime.fromisoformat(d["ts"]) if isinstance(d["ts"], str) else d["ts"], d["open"], d["high"],
                       d["low"], d["close"], d["volume"], d.get("oi"), tf) for d in c.runtime.candles(inst.key, tf, 500)]
        used = "engine"
    specs = [parse_indicator(s, tf) for s in ind]
    df = compute_frame(rows, specs) if rows else None
    records = [] if df is None else [{k_: _clean(v) for k_, v in r.items()} for r in df.to_dict("records")]
    closed = [r for r in rows if tfm.bucket_end(r.ts, tf) <= now_ist()] or rows
    pa = price_action.analyze(closed, tf) if len(closed) > 20 else None
    markers = []
    for j in container().repo.journal(300):
        if j["instrument"] == inst.key or (j.get("underlying") and j["underlying"] == inst.name and not inst.is_option):
            label = (j.get("action") or "").replace("_", " ")
            if j.get("strike"):
                label += f" {j['strike']:g}"
            markers.append({"time": j["entry_time"], "kind": "entry", "label": label, "action": j.get("action"),
                            "price": j.get("underlying_entry") if j["instrument"] != inst.key else j["entry_price"]})
            if j.get("exit_time"):
                markers.append({"time": j["exit_time"], "kind": "exit", "action": j.get("action"),
                                "label": f"exit {j.get('net_pnl', 0):+,.0f}", "pnl": j.get("net_pnl"),
                                "price": j.get("underlying_exit") if j["instrument"] != inst.key else j["exit_price"]})
    return {
        "instrument": inst.key, "token": inst.token, "timeframe": tf, "source": used, "candles": records,
        "markers": markers,
        "candlestick_patterns": [p.as_dict() for p in candlestick.detect_recent(closed[-40:], 5, tf)] if closed else [],
        "chart_patterns": [p.as_dict() for p in chart_patterns.detect(closed, tf)] if closed else [],
        "price_action": {"trend": pa.trend, "atr": pa.atr, "nearest_support": pa.nearest_support,
                         "nearest_resistance": pa.nearest_resistance, "levels": [asdict(l) for l in pa.levels[-12:]],
                         "events": [e.as_dict() for e in pa.events]} if pa else None,
    }


@router.get("/option-chain")
def option_chain(underlying: str, expiry: date | None = None, width: int = 12, k=Depends(kite)):
    c = container()
    reg = c.instruments.registry()
    u = underlying.upper()
    futs = reg.futures(u)
    exps = reg.option_expiries(u)
    if not exps:
        raise HTTPException(404, f"No options for {u}")
    expiry = expiry or exps[0]
    if not futs:
        raise HTTPException(404, f"No futures for {u} (needed as the forward price)")
    # Forward price from the near future (r=0 in Black-Scholes); avoids dividend/rate assumptions.
    fq = k.quote([futs[0].key]).get(futs[0].key) or {}
    spot = float(fq.get("last_price") or 0)
    if not spot:
        raise HTTPException(502, "No price for the underlying future")
    contracts = chain_instruments(reg, u, expiry, spot, width=width)
    quotes: dict = {}
    keys = [x.key for x in contracts]
    for i in range(0, len(keys), 450):
        quotes.update(k.quote(keys[i:i + 450]))
    today = date.today()
    base = {}
    for x in contracts:
        oi = (quotes.get(x.key) or {}).get("oi")
        if oi is not None:
            base[x.token] = c.oi_baseline.setdefault((today, x.token), oi)
    chain = build_chain(contracts, quotes, spot, datetime.now(IST), rate=0.0, oi_baseline=base)
    chain.update({"underlying": u, "expiry": expiry, "expiries": exps, "future": futs[0].key,
                  "oi_change_basis": "since first chain fetch today (Kite quotes have no previous-day OI)"})
    return chain


class WatchRequest(BaseModel):
    tokens: list[int]


@router.post("/watch")
def watch(req: WatchRequest):
    container().runtime.watch(req.tokens)
    return {"ok": True}


@router.post("/unwatch")
def unwatch(req: WatchRequest):
    container().runtime.unwatch(req.tokens)
    return {"ok": True}
