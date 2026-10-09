"""User watchlists stored in this app.

Kite Connect has no API for the watchlists inside the Kite app, so lists live here. They can be
filled by search, imported from Zerodha holdings/positions, or pasted as a list of symbols.

Prices come from the live WebSocket feed (FULL-mode ticks) for every watched instrument; a REST
quote snapshot (cached, rate-limited) fills gaps, e.g. outside market hours or before the first tick.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.models import Instrument
from app.market_data.instruments import InstrumentRegistry
from backend.deps import container

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

INDEX_ALIASES = {
    "NIFTY": "NSE:NIFTY 50", "NIFTY50": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "SENSEX": "BSE:SENSEX", "BANKEX": "BSE:BANKEX", "VIX": "NSE:INDIA VIX",
    "INDIAVIX": "NSE:INDIA VIX",
}
DEFAULT_INDICES = ["NSE:NIFTY 50", "NSE:NIFTY BANK", "BSE:SENSEX", "NSE:NIFTY FIN SERVICE", "NSE:NIFTY MID SELECT",
                   "NSE:INDIA VIX"]
KINDS = {"equity": ("EQ",), "index": ("INDEX",), "future": ("FUT",), "option": ("CE", "PE")}


def _registry() -> InstrumentRegistry:
    c = container()
    if c.session.logged_in:
        return c.instruments.registry()
    if c.runtime.engine is not None:          # synthetic session: its own instruments
        return c.runtime.engine.registry
    raise HTTPException(401, "Log in with Kite to search instruments")


def _item(i: Instrument) -> dict:
    return {"key": i.key, "token": i.token, "exchange": i.exchange, "tradingsymbol": i.tradingsymbol, "name": i.name,
            "instrument_type": i.instrument_type, "segment": i.segment, "expiry": i.expiry, "strike": i.strike,
            "lot_size": i.lot_size}


def _watch(tokens: list[int]) -> None:
    rt = container().runtime
    try:
        rt.watch(tokens)
    except Exception:
        pass  # engine not running yet; tokens are subscribed when it starts


def _ensure_default() -> None:
    repo = container().repo
    if repo.watchlists():
        return
    wid = repo.create_watchlist("Indices")
    try:
        reg = _registry()
        items = [_item(reg.get(k)) for k in DEFAULT_INDICES if k in reg]
        if items:
            repo.add_watchlist_items(wid, items)
            _watch([i["token"] for i in items])
    except HTTPException:
        pass


# ---------------------------------------------------------------- lists

class NameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)


@router.get("")
def lists():
    _ensure_default()
    return container().repo.watchlists()


@router.post("")
def create(req: NameRequest):
    repo = container().repo
    if any(w["name"].lower() == req.name.strip().lower() for w in repo.watchlists()):
        raise HTTPException(409, f"A watchlist named {req.name!r} already exists")
    return {"id": repo.create_watchlist(req.name.strip())}


@router.patch("/{wid}")
def rename(wid: int, req: NameRequest):
    container().repo.rename_watchlist(wid, req.name.strip())
    return {"ok": True}


@router.delete("/{wid}")
def delete(wid: int):
    container().repo.delete_watchlist(wid)
    return {"ok": True}


# ---------------------------------------------------------------- items

class KeysRequest(BaseModel):
    keys: list[str]


@router.get("/search")
def search(q: str, exchange: str | None = None, kind: Literal["equity", "index", "future", "option"] | None = None,
           limit: int = 40):
    reg = _registry()
    res = reg.search(q, exchange.upper() if exchange else None, limit=limit, types=KINDS[kind] if kind else None)
    return [_item(i) for i in res]


@router.get("/{wid}/items")
def items(wid: int):
    return container().repo.watchlist_items(wid)


@router.post("/{wid}/items")
def add(wid: int, req: KeysRequest):
    reg = _registry()
    found = [_item(reg.get(k)) for k in req.keys if k in reg]
    missing = [k for k in req.keys if k not in reg]
    n = container().repo.add_watchlist_items(wid, found)
    _watch([i["token"] for i in found])
    return {"added": n, "not_found": missing}


@router.post("/{wid}/items/remove")
def remove(wid: int, req: KeysRequest):
    repo = container().repo
    gone = [i for i in repo.watchlist_items(wid) if i["instrument_key"] in req.keys]
    repo.remove_watchlist_items(wid, req.keys)
    still = set(repo.all_watchlist_tokens())
    drop = [i["token"] for i in gone if i["token"] not in still]
    if drop:
        try:
            container().runtime.unwatch(drop)
        except Exception:
            pass
    return {"removed": len(gone)}


class ImportRequest(BaseModel):
    source: Literal["holdings", "positions", "symbols"]
    symbols: str = ""


def _resolve_symbol(reg: InstrumentRegistry, raw: str) -> Instrument | None:
    s = raw.strip().upper()
    if not s:
        return None
    if ":" in s:
        return reg.get(s) if s in reg else None
    alias = INDEX_ALIASES.get(s.replace(" ", ""))
    if alias and alias in reg:
        return reg.get(alias)
    for ex in ("NSE", "BSE", "NFO", "BFO"):
        if f"{ex}:{s}" in reg:
            return reg.get(f"{ex}:{s}")
    hits = [i for i in reg.search(s, limit=5) if i.tradingsymbol == s]
    return hits[0] if hits else None


@router.post("/{wid}/import")
def import_items(wid: int, req: ImportRequest):
    c = container()
    reg = _registry()
    keys: list[str] = []
    if req.source in ("holdings", "positions"):
        if not c.session.logged_in:
            raise HTTPException(401, "Log in with Kite to import holdings/positions")
        rows = c.session.kite.holdings() if req.source == "holdings" else c.session.kite.positions().get("net", [])
        keys = [f"{r['exchange']}:{r['tradingsymbol']}" for r in rows]
        insts = [reg.get(k) for k in keys if k in reg]
        missing = [k for k in keys if k not in reg]
    else:
        parts = [p for p in re.split(r"[,\n;\t]+", req.symbols) if p.strip()]
        insts, missing = [], []
        for p in parts:
            i = _resolve_symbol(reg, p)
            (insts.append(i) if i else missing.append(p.strip()))
    found = [_item(i) for i in insts]
    n = c.repo.add_watchlist_items(wid, found)
    _watch([i["token"] for i in found])
    return {"added": n, "not_found": missing}


# ---------------------------------------------------------------- live quotes

_snap_cache: dict[str, tuple[float, dict]] = {}
_snap_lock = threading.Lock()
_last_quote_call = [0.0]


def _snapshots(keys: list[str], max_age: float = 60.0) -> dict[str, dict]:
    """REST quote snapshots (circuit limits, and prices when no tick has arrived). Cached; <= 1 call / 2 s."""
    c = container()
    now = time.monotonic()
    out = {k: v for k, (t, v) in _snap_cache.items() if k in keys and now - t < max_age}
    stale = [k for k in keys if k not in out]
    if stale and c.session.logged_in and now - _last_quote_call[0] >= 2.0:
        with _snap_lock:
            _last_quote_call[0] = time.monotonic()
            try:
                for i in range(0, len(stale), 450):
                    res = c.session.kite.quote(stale[i:i + 450])
                    for k, v in res.items():
                        _snap_cache[k] = (time.monotonic(), v)
                        out[k] = v
            except Exception:
                pass
    return out


@router.get("/{wid}/quotes")
def quotes(wid: int):
    c = container()
    rows = c.repo.watchlist_items(wid)
    ticks = c.runtime.latest_ticks
    snaps = _snapshots([r["instrument_key"] for r in rows])
    out = []
    for r in rows:
        key = r["instrument_key"]
        snap = snaps.get(key) or {}
        ohlc = snap.get("ohlc") or {}
        depth = snap.get("depth") or {}
        d = {"key": key, "symbol": r["tradingsymbol"], "exchange": r["exchange"], "name": r["name"],
             "type": r["instrument_type"], "segment": r["segment"], "expiry": r["expiry"], "strike": r["strike"],
             "lot_size": r["lot_size"],
             "ltp": snap.get("last_price"), "open": ohlc.get("open"), "high": ohlc.get("high"), "low": ohlc.get("low"),
             "prev_close": ohlc.get("close"), "volume": snap.get("volume"), "avg_price": snap.get("average_price"),
             "oi": snap.get("oi"), "bid": ((depth.get("buy") or [{}])[0]).get("price") or None,
             "ask": ((depth.get("sell") or [{}])[0]).get("price") or None,
             "upper_circuit": snap.get("upper_circuit_limit"), "lower_circuit": snap.get("lower_circuit_limit"),
             "updated": str(snap.get("last_trade_time") or "")[11:19] or None, "live": False}
        t = ticks.get(r["token"])
        if t:   # WebSocket tick is fresher than the snapshot
            for k_t, k_d in (("ltp", "ltp"), ("open", "open"), ("high", "high"), ("low", "low"),
                             ("prev_close", "prev_close"), ("volume", "volume"), ("vwap", "avg_price"), ("oi", "oi"),
                             ("bid", "bid"), ("ask", "ask")):
                if t.get(k_t) not in (None, 0):
                    d[k_d] = t[k_t]
            d["updated"], d["live"] = (t.get("ts") or "")[11:19], True
        ltp, pc = d["ltp"], d["prev_close"]
        d["change"] = round(ltp - pc, 2) if (ltp and pc) else None
        d["change_pct"] = round(100 * (ltp - pc) / pc, 2) if (ltp and pc) else None
        hi, lo = d["high"], d["low"]
        d["day_range_pos"] = round((ltp - lo) / (hi - lo), 3) if (ltp and hi and lo and hi > lo) else None
        out.append(d)
    return out
