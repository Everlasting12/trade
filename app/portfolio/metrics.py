"""Performance statistics shared by the backtester, paper trading and the strategy monitor."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from typing import Iterable

from app.portfolio.portfolio import Trade


def _streaks(pnls: list[float]) -> tuple[int, int]:
    best_w = best_l = cur_w = cur_l = 0
    for p in pnls:
        if p > 0:
            cur_w, cur_l = cur_w + 1, 0
        elif p < 0:
            cur_l, cur_w = cur_l + 1, 0
        else:
            cur_w = cur_l = 0
        best_w, best_l = max(best_w, cur_w), max(best_l, cur_l)
    return best_w, best_l


def max_drawdown(curve: list[tuple[datetime, float]]) -> tuple[float, float]:
    peak, mdd, mdd_pct = -math.inf, 0.0, 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        dd = peak - eq
        if dd > mdd:
            mdd, mdd_pct = dd, (100 * dd / peak if peak > 0 else 0.0)
    return mdd, mdd_pct


def daily_returns(curve: list[tuple[datetime, float]]) -> list[float]:
    """Close-of-day equity -> daily simple returns."""
    eod: dict = {}
    for ts, eq in curve:
        eod[ts.date()] = eq
    vals = [eod[d] for d in sorted(eod)]
    return [(b - a) / a for a, b in zip(vals, vals[1:]) if a]


def compute(trades: Iterable[Trade], curve: list[tuple[datetime, float]], capital: float) -> dict:
    trades = list(trades)
    pnls = [t.net_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    final = curve[-1][1] if curve else capital
    total_return = (final - capital) / capital if capital else 0.0
    cagr = None
    if len(curve) >= 2:
        days = (curve[-1][0] - curve[0][0]).total_seconds() / 86400
        if days >= 1 and final > 0:
            cagr = (final / capital) ** (365.25 / days) - 1
    dr = daily_returns(curve)
    sharpe = None
    if len(dr) >= 2:
        mean = sum(dr) / len(dr)
        sd = math.sqrt(sum((r - mean) ** 2 for r in dr) / (len(dr) - 1))
        sharpe = (mean / sd) * math.sqrt(252) if sd > 0 else None
    mdd, mdd_pct = max_drawdown(curve)
    gross_win, gross_loss = sum(wins), -sum(losses)
    max_w, max_l = _streaks(pnls)
    n = len(trades)
    return {
        "starting_capital": round(capital, 2),
        "final_equity": round(final, 2),
        "net_pnl": round(final - capital, 2),
        "total_return_pct": round(100 * total_return, 2),
        "cagr_pct": round(100 * cagr, 2) if cagr is not None else None,
        "num_trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 2) if n else 0.0,
        "loss_rate_pct": round(100 * len(losses) / n, 2) if n else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else (None if not gross_win else math.inf),
        "max_drawdown": round(mdd, 2),
        "max_drawdown_pct": round(mdd_pct, 2),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "avg_trade": round(sum(pnls) / n, 2) if n else 0.0,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(pnls) / n, 2) if n else 0.0,
        "max_consecutive_wins": max_w,
        "max_consecutive_losses": max_l,
        "total_charges": round(sum(t.charges for t in trades), 2),
    }


def by_strategy(trades: Iterable[Trade]) -> dict[str, dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[t.strategy].append(t)
    out = {}
    for s, ts in groups.items():
        pnls = [t.net_pnl for t in ts]
        eq, peak, mdd = 0.0, 0.0, 0.0
        for p in pnls:
            eq += p
            peak = max(peak, eq)
            mdd = max(mdd, peak - eq)
        out[s] = {"trades": len(ts), "win_rate_pct": round(100 * sum(p > 0 for p in pnls) / len(ts), 2),
                  "net_pnl": round(sum(pnls), 2), "max_drawdown": round(mdd, 2)}
    return out
