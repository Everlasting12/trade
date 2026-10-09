"""Risk manager: independent of strategies, has the final say on every new position.

Strategy -> Signal -> Scorer -> **RiskManager** -> OMS -> Broker

- Entries are approved only if every check passes; the decision lists every check and its outcome.
- Exits are always allowed (they reduce risk), even with the kill switch active.
- Position size is derived from the stop distance so that a stop-out loses at most
  `max_risk_per_trade` (before slippage/gaps), rounded down to whole lots.
- Breaching the daily loss / consecutive-loss limits trips a *daily halt*: no new entries for the rest
  of the day. The global kill switch blocks entries until manually reset.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

from app.core.clock import parse_hhmm
from app.core.models import AccountBalance, Instrument, Signal, SignalAction
from app.portfolio.portfolio import Portfolio


@dataclass
class StrategyLimits:
    max_trades_per_day: int | None = None
    max_daily_loss: float | None = None
    max_open_positions: int | None = None
    max_risk_per_trade: float | None = None


@dataclass
class RiskConfig:
    max_daily_loss: float = 2000.0
    max_risk_per_trade: float = 500.0
    max_trades_per_day: int = 5
    max_open_positions: int = 3
    max_consecutive_losses: int = 3
    max_capital_utilization: float = 0.8       # (used margin + new margin) / capital
    max_position_lots: int = 10
    max_position_notional: float = 5_000_000.0
    require_stop_loss: bool = True
    min_risk_reward: float = 1.0
    max_stop_distance_pct: float = 5.0
    allow_short: bool = True
    allow_option_selling: bool = False
    min_option_premium: float = 5.0            # avoid illiquid "lottery" contracts
    max_premium_per_trade: float = 50_000.0    # cap on premium paid for one option buy
    max_short_option_lots: int = 2             # cap on lots for one option sell (unbounded-risk side)
    max_option_stop_pct: float = 100.0         # option stops are % of premium (a short's stop can be 2x premium)
    no_entries_before: str = "09:20"
    no_new_entries_after: str = "15:00"
    square_off_time: str = "15:15"             # intraday positions are closed at this time
    stale_data_seconds: float = 15.0
    max_drawdown_pct: float | None = None      # portfolio-level: trip the kill switch beyond this
    flatten_on_daily_halt: bool = True
    strategy_limits: dict[str, StrategyLimits] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None, **overrides) -> "RiskConfig":
        d = {**(d or {}), **{k: v for k, v in overrides.items() if v is not None}}
        sl = {k: StrategyLimits(**v) for k, v in (d.pop("strategy_limits", None) or {}).items()}
        known = {f for f in cls.__dataclass_fields__}
        return cls(strategy_limits=sl, **{k: v for k, v in d.items() if k in known})


@dataclass
class RiskCheck:
    check: str
    passed: bool
    detail: str = ""


@dataclass
class RiskDecision:
    approved: bool
    quantity: int = 0
    checks: list[RiskCheck] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.check}: {c.detail}" for c in self.checks if not c.passed]

    def as_dict(self) -> dict:
        return {"approved": self.approved, "quantity": self.quantity, "reasons": self.reasons,
                "checks": [vars(c) for c in self.checks]}


@dataclass
class RiskInputs:
    """Everything the risk manager needs, assembled by the engine. No hidden global state."""
    now: datetime
    instrument: Instrument
    portfolio: Portfolio
    account: AccountBalance
    last_price: float
    data_age_seconds: float = 0.0
    pending_entry_instruments: frozenset[str] = frozenset()
    margin_for_qty: Callable[[int], float] | None = None
    enforce_session_times: bool = True


@dataclass
class KillSwitch:
    active: bool = False
    reason: str = ""
    ts: datetime | None = None


class RiskManager:
    def __init__(self, config: RiskConfig | None = None):
        self.cfg = config or RiskConfig()
        self.kill_switch = KillSwitch()
        self.daily_halts: dict[date, str] = {}

    # ---------------------------------------------------------------- controls
    def trip_kill_switch(self, reason: str, now: datetime) -> None:
        self.kill_switch = KillSwitch(True, reason, now)

    def reset_kill_switch(self) -> None:
        self.kill_switch = KillSwitch()

    def halted(self, day: date) -> str | None:
        return self.daily_halts.get(day)

    def check_breaches(self, now: datetime, portfolio: Portfolio) -> str | None:
        """Called on every candle/timer. Returns a new halt reason the first time a limit is breached."""
        day = now.date()
        if day in self.daily_halts:
            return None
        reason = None
        pnl = portfolio.day_pnl(day)
        if pnl <= -self.cfg.max_daily_loss:
            reason = f"Daily loss {pnl:,.0f} breached limit -{self.cfg.max_daily_loss:,.0f}"
        elif portfolio.consecutive_losses(day) >= self.cfg.max_consecutive_losses:
            reason = f"{portfolio.consecutive_losses(day)} consecutive losses (limit {self.cfg.max_consecutive_losses})"
        if reason:
            self.daily_halts[day] = reason
        if self.cfg.max_drawdown_pct is not None and not self.kill_switch.active:
            _, dd_pct = portfolio.drawdown_now()
            if dd_pct >= self.cfg.max_drawdown_pct:
                self.trip_kill_switch(f"Drawdown {dd_pct:.1f}% >= {self.cfg.max_drawdown_pct}%", now)
                return self.kill_switch.reason
        return reason

    # ---------------------------------------------------------------- sizing
    def size(self, signal: Signal, inst: Instrument, max_risk: float) -> tuple[int, str]:
        rpu = signal.risk_per_unit
        lot = inst.lot_size
        if rpu is None or rpu <= 0:
            if signal.quantity:
                return signal.quantity, "strategy quantity (no stop to size from)"
            return lot, "1 lot (no stop to size from)"
        lots = math.floor(max_risk / (rpu * lot))
        lots = min(lots, self.cfg.max_position_lots)
        if signal.entry_price and inst.is_option:
            if signal.action == SignalAction.BUY:
                lots = min(lots, math.floor(self.cfg.max_premium_per_trade / (signal.entry_price * lot)))
            else:
                lots = min(lots, self.cfg.max_short_option_lots)
        elif signal.entry_price:
            lots = min(lots, math.floor(self.cfg.max_position_notional / (signal.entry_price * lot)))
        qty = lots * lot
        if signal.quantity:
            qty = min(qty, (signal.quantity // lot) * lot)
        return max(qty, 0), f"{lots} lot(s) x {lot}: risk/unit {rpu:.2f}, max risk {max_risk:,.0f}"

    # ---------------------------------------------------------------- evaluation
    def evaluate(self, signal: Signal, inp: RiskInputs) -> RiskDecision:
        checks: list[RiskCheck] = []
        pf, inst, now, cfg = inp.portfolio, inp.instrument, inp.now, self.cfg
        day = now.date()
        pos = pf.positions.get(signal.instrument)

        def chk(name: str, ok: bool, detail: str = "") -> bool:
            checks.append(RiskCheck(name, bool(ok), detail))
            return bool(ok)

        if signal.action == SignalAction.EXIT:
            ok = chk("has_position", pos is not None and pos.strategy == signal.strategy,
                     "no open position for this strategy" if not pos else f"position {pos.quantity}")
            return RiskDecision(ok, abs(pos.quantity) if (ok and pos) else 0, checks)

        long = signal.action == SignalAction.BUY
        sl_cfg = cfg.strategy_limits.get(signal.strategy, StrategyLimits())

        chk("kill_switch", not self.kill_switch.active, self.kill_switch.reason or "off")
        chk("daily_halt", day not in self.daily_halts, self.daily_halts.get(day, "none"))
        if inp.enforce_session_times and signal.timeframe != "1d":
            t = now.time()
            inside = parse_hhmm(cfg.no_entries_before) <= t < parse_hhmm(cfg.no_new_entries_after)
            chk("entry_window", inside, f"{t:%H:%M} {'inside' if inside else 'outside'} "
                                        f"{cfg.no_entries_before}-{cfg.no_new_entries_after}")
        chk("market_data_fresh", inp.data_age_seconds <= cfg.stale_data_seconds,
            f"last tick {inp.data_age_seconds:.0f}s ago (max {cfg.stale_data_seconds:.0f}s)")
        chk("tradable", inst.tradable, inst.key)
        if not long:
            chk("short_allowed", cfg.allow_short, "short selling disabled")
            if inst.is_option:
                chk("option_selling", cfg.allow_option_selling,
                    "option selling allowed" if cfg.allow_option_selling else "option selling disabled in risk config")
        chk("no_existing_position", pos is None, f"already {pos.side} {abs(pos.quantity)} ({pos.strategy})" if pos else "")
        pending = signal.instrument in inp.pending_entry_instruments
        chk("no_pending_entry", not pending, "entry order already working" if pending else "")

        entry = signal.entry_price or inp.last_price
        if signal.entry_price is None:
            signal.entry_price = entry
        if inst.is_option:
            chk("min_option_premium", entry >= cfg.min_option_premium,
                f"premium {entry:.2f} (min {cfg.min_option_premium})")
        if cfg.require_stop_loss:
            chk("stop_loss_present", signal.stop_loss is not None, "stop loss required")
        if signal.stop_loss is not None:
            right_side = signal.stop_loss < entry if long else signal.stop_loss > entry
            chk("stop_loss_side", right_side, f"SL {signal.stop_loss:.2f} vs entry {entry:.2f}")
            dist_pct = 100 * abs(entry - signal.stop_loss) / entry if entry else 0
            max_pct = cfg.max_option_stop_pct if inst.is_option else cfg.max_stop_distance_pct
            chk("stop_distance", dist_pct <= max_pct, f"{dist_pct:.2f}% (max {max_pct}%)")
        if signal.target is not None:
            right_side = signal.target > entry if long else signal.target < entry
            chk("target_side", right_side, f"target {signal.target:.2f} vs entry {entry:.2f}")
            rr = signal.risk_reward
            if rr is not None:
                chk("risk_reward", rr >= cfg.min_risk_reward, f"R:R {rr} (min {cfg.min_risk_reward})")

        max_risk = min(cfg.max_risk_per_trade, sl_cfg.max_risk_per_trade or math.inf)
        # Never risk more than what is left of today's loss budget.
        day_pnl = pf.day_pnl(day)
        budget_left = cfg.max_daily_loss + min(0.0, day_pnl)
        max_risk = min(max_risk, budget_left)
        qty, how = self.size(signal, inst, max_risk) if max_risk > 0 else (0, "no daily loss budget left")
        if qty and inp.margin_for_qty:
            # Shrink (never grow) the risk-based size to what margin and the utilisation cap allow.
            acct = inp.account
            room = min(acct.available_margin,
                       cfg.max_capital_utilization * pf.capital - acct.used_margin)
            lot = inst.lot_size
            risk_qty = qty
            while qty > 0 and inp.margin_for_qty(qty) > room:
                qty -= lot
            if qty <= 0:
                how = f"margin for 1 lot {inp.margin_for_qty(lot):,.0f} exceeds room {max(room, 0):,.0f}"
            elif qty < risk_qty:
                how += f"; capped to {qty // lot} lot(s) by margin"
        chk("position_size", qty > 0, how if qty > 0 else f"cannot size: {how}")
        if qty and signal.risk_per_unit:
            risk = signal.risk_per_unit * qty
            chk("risk_per_trade", risk <= max_risk + 1e-6, f"{risk:,.0f} (max {max_risk:,.0f})")

        trades_today = pf.entries_on(day)
        chk("max_trades_per_day", trades_today < cfg.max_trades_per_day, f"{trades_today}/{cfg.max_trades_per_day}")
        chk("max_open_positions", len(pf.positions) < cfg.max_open_positions,
            f"{len(pf.positions)}/{cfg.max_open_positions}")
        chk("daily_loss", day_pnl > -cfg.max_daily_loss, f"day P&L {day_pnl:,.0f} (limit -{cfg.max_daily_loss:,.0f})")
        closs = pf.consecutive_losses(day)
        chk("consecutive_losses", closs < cfg.max_consecutive_losses, f"{closs} (max {cfg.max_consecutive_losses})")

        if sl_cfg.max_trades_per_day is not None:
            n = pf.entries_on(day, signal.strategy)
            chk("strategy_trades_per_day", n < sl_cfg.max_trades_per_day, f"{n}/{sl_cfg.max_trades_per_day}")
        if sl_cfg.max_daily_loss is not None:
            sp = pf.day_pnl(day, signal.strategy)
            chk("strategy_daily_loss", sp > -sl_cfg.max_daily_loss, f"{sp:,.0f} (limit -{sl_cfg.max_daily_loss:,.0f})")
        if sl_cfg.max_open_positions is not None:
            n = sum(1 for p in pf.positions.values() if p.strategy == signal.strategy)
            chk("strategy_open_positions", n < sl_cfg.max_open_positions, f"{n}/{sl_cfg.max_open_positions}")

        if qty and inp.margin_for_qty:
            need = inp.margin_for_qty(qty)
            acct = inp.account
            chk("margin_available", need <= acct.available_margin,
                f"need {need:,.0f}, available {acct.available_margin:,.0f}")
            util = (acct.used_margin + need) / pf.capital if pf.capital else 1.0
            chk("capital_utilization", util <= cfg.max_capital_utilization,
                f"{100 * util:.0f}% (max {100 * cfg.max_capital_utilization:.0f}%)")

        approved = all(c.passed for c in checks)
        return RiskDecision(approved, qty if approved else 0, checks)
