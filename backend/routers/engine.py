"""Engine control and monitoring. All mutating calls execute on the engine's worker thread."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.deps import container

router = APIRouter(prefix="/engine", tags=["engine"])


def _rt():
    return container().runtime


def _guard(fn):
    try:
        return fn()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))


@router.get("/snapshot")
def snapshot():
    return _rt().snapshot()


@router.get("/status")
def status():
    rt = _rt()
    return {"status": rt.status, "error": rt.error, "mode": str(rt.mode), "settings": container().settings.public()}


@router.post("/start")
def start():
    rt = _rt()
    rt.start()
    return {"status": rt.status, "error": rt.error}


@router.post("/restart")
def restart():
    rt = _rt()
    rt.restart()
    return {"status": rt.status, "error": rt.error}


class KillRequest(BaseModel):
    reason: str = Field(default="manual kill switch", max_length=200)
    flatten: bool = True


@router.post("/kill-switch")
def kill(req: KillRequest):
    _guard(lambda: _rt().kill_switch(req.reason, req.flatten))
    return {"ok": True}


class ResetRequest(BaseModel):
    confirm: bool


@router.post("/kill-switch/reset")
def reset_kill(req: ResetRequest):
    if not req.confirm:
        raise HTTPException(400, "confirm must be true")
    _guard(lambda: _rt().reset_kill_switch())
    return {"ok": True}


@router.post("/strategies/{sid}/{action}")
def strategy_toggle(sid: str, action: Literal["enable", "disable"]):
    _guard(lambda: _rt().set_strategy_enabled(sid, action == "enable"))
    return {"ok": True}


class ExitRequest(BaseModel):
    instrument: str
    reason: str = "manual exit"


@router.post("/positions/exit")
def exit_position(req: ExitRequest):
    return {"order": _guard(lambda: _rt().exit_position(req.instrument, req.reason))}


class ManualOrder(BaseModel):
    instrument: str
    action: Literal["BUY", "SELL", "EXIT"]
    stop_loss: float | None = None
    target: float | None = None
    quantity: int | None = None
    entry_price: float | None = None
    product: Literal["MIS", "NRML"] | None = None
    reason: str | None = None


@router.post("/manual-order")
def manual_order(req: ManualOrder):
    return _guard(lambda: _rt().manual_signal(req.model_dump()))


class ArmRequest(BaseModel):
    phrase: str
    operator: str = Field(default="operator", max_length=60)


@router.post("/live/arm")
def arm(req: ArmRequest):
    return _guard(lambda: _rt().arm_live(req.phrase, req.operator))


@router.post("/live/disarm")
def disarm():
    _guard(lambda: _rt().disarm_live())
    return {"ok": True}


class AckRequest(BaseModel):
    operator: str = "operator"


@router.post("/live/acknowledge-reconciliation")
def ack(req: AckRequest):
    _guard(lambda: _rt().acknowledge_reconciliation(req.operator))
    return {"ok": True}


class AutoTradingRequest(BaseModel):
    on: bool


@router.post("/auto-trading")
def auto_trading(req: AutoTradingRequest):
    _guard(lambda: _rt().set_auto_trading(req.on))
    return {"ok": True, "auto_trading": req.on}


@router.get("/option-chain")
def option_chain(underlying: str, expiry: str | None = None, width: int = 12):
    """Option chain via the engine's option market: Kite quotes, or the pricing model in synthetic mode."""
    return _guard(lambda: _rt().option_chain(underlying.upper(), expiry, width))


@router.get("/journal")
def journal(limit: int = 500, since: str | None = None, status: Literal["open", "closed"] | None = None):
    return container().repo.journal(limit, since, status)


def exit_category(reason: str | None) -> str:
    r = (reason or "").lower()
    for key, label in (("underlying stop", "underlying stop"), ("underlying target", "underlying target"),
                       ("stop_loss", "premium stop"), ("stop-limit", "premium stop"), ("target", "premium target"),
                       ("square-off", "square-off"), ("expiry", "expiry-day exit"), ("strategy exit", "strategy exit"),
                       ("kill switch", "kill switch"), ("daily halt", "daily halt"), ("manual", "manual"),
                       ("end of", "end of data")):
        if key in r:
            return label
    return r or "-"


@router.get("/journal/stats")
def journal_stats(since: str | None = None):
    rows = [r for r in container().repo.journal(5000, since) if r["status"] == "closed"]

    def summ(rs):
        n = len(rs)
        pnl = [r["net_pnl"] or 0 for r in rs]
        rr = [r["r_multiple"] for r in rs if r.get("r_multiple") is not None]
        wins = [p for p in pnl if p > 0]
        losses = [p for p in pnl if p < 0]
        return {"trades": n, "net_pnl": round(sum(pnl), 2), "win_rate_pct": round(100 * len(wins) / n, 1) if n else 0.0,
                "avg_r": round(sum(rr) / len(rr), 2) if rr else None,
                "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
                "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
                "profit_factor": round(sum(wins) / -sum(losses), 2) if losses else None}

    def group(key):
        out = {}
        for r in rows:
            k = exit_category(r.get(key)) if key == "exit_reason" else (r.get(key) or "-")
            out.setdefault(k, []).append(r)
        return {k: summ(v) for k, v in sorted(out.items())}

    by_day: dict = {}
    for r in rows:
        by_day.setdefault((r["entry_time"] or "")[:10], []).append(r)
    return {"overall": summ(rows), "by_action": group("action"), "by_strategy": group("strategy"),
            "by_underlying": group("underlying"), "by_exit_reason": group("exit_reason"),
            "by_day": {d: summ(v) for d, v in sorted(by_day.items())}}


@router.get("/candles")
def candles(instrument: str, tf: str = "5m", n: int = 300):
    return _guard(lambda: _rt().candles(instrument, tf, n))


@router.get("/history/{kind}")
def history(kind: Literal["trades", "orders", "signals", "events"], limit: int = 500, level: str | None = None):
    repo = container().repo
    if kind == "trades":
        return repo.trades(limit)
    if kind == "orders":
        return repo.orders(limit)
    if kind == "signals":
        return repo.signals(limit)
    return repo.events(limit, level)
