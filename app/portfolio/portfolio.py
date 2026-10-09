"""Internal book of record: strategy-owned positions, closed round-trip trades, P&L and equity.

The broker (paper or Zerodha) is the source of truth for *what is held*; this portfolio is the source
of truth for *why* (which strategy, stop, target) and for analytics. In live mode the two are
reconciled on startup and periodically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime

from app.core.models import Fill, new_id


@dataclass
class ManagedPosition:
    instrument: str
    strategy: str
    quantity: int                      # signed
    avg_price: float
    opened_at: datetime
    stop_loss: float | None = None
    target: float | None = None
    last_price: float = 0.0
    entry_charges: float = 0.0
    signal_id: str | None = None
    position_id: str = field(default_factory=lambda: new_id("pos_"))
    max_favorable: float = 0.0         # MFE per unit
    max_adverse: float = 0.0           # MAE per unit

    @property
    def side(self) -> str:
        return "LONG" if self.quantity > 0 else "SHORT"

    @property
    def unrealized(self) -> float:
        return (self.last_price - self.avg_price) * self.quantity if self.last_price else 0.0

    def as_dict(self, now: datetime | None = None) -> dict:
        d = asdict(self)
        d["side"] = self.side
        d["unrealized_pnl"] = round(self.unrealized, 2)
        notional = abs(self.avg_price * self.quantity)
        d["pnl_pct"] = round(100 * self.unrealized / notional, 2) if notional else 0.0
        if now:
            d["duration_s"] = int((now - self.opened_at).total_seconds())
        return d


@dataclass
class Trade:
    trade_id: str
    position_id: str
    strategy: str
    instrument: str
    side: str
    quantity: int
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    gross_pnl: float
    charges: float
    exit_reason: str = ""
    mfe: float = 0.0
    mae: float = 0.0

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.charges

    @property
    def pnl_pct(self) -> float:
        notional = self.entry_price * self.quantity
        return 100 * self.net_pnl / notional if notional else 0.0

    def as_dict(self) -> dict:
        d = asdict(self)
        d["net_pnl"] = round(self.net_pnl, 2)
        d["pnl_pct"] = round(self.pnl_pct, 3)
        d["duration_s"] = int((self.exit_ts - self.entry_ts).total_seconds())
        return d


class Portfolio:
    def __init__(self, capital: float):
        self.capital = capital
        self.positions: dict[str, ManagedPosition] = {}
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []
        self.realized_gross = 0.0
        self.total_charges = 0.0
        self.entries: list[tuple[date, str, str]] = []       # (day, strategy, instrument) per opened position
        self._peak = capital
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0

    # ---------------------------------------------------------------- updates
    def on_fill(self, fill: Fill, stop_loss: float | None = None, target: float | None = None,
                signal_id: str | None = None, exit_reason: str = "") -> list[Trade]:
        """Apply a fill. Returns trades closed by it (0, 1, or 1 + a flip)."""
        self.total_charges += fill.charges
        signed = fill.quantity * fill.side.sign
        pos = self.positions.get(fill.instrument)
        closed: list[Trade] = []
        if pos is None or pos.quantity == 0:
            self._open(fill, signed, fill.charges, stop_loss, target, signal_id)
            return closed
        if (pos.quantity > 0) == (signed > 0):  # adding to position
            new = pos.quantity + signed
            pos.avg_price = (pos.avg_price * abs(pos.quantity) + fill.price * abs(signed)) / abs(new)
            pos.quantity = new
            pos.entry_charges += fill.charges
            if stop_loss is not None:
                pos.stop_loss = stop_loss
            if target is not None:
                pos.target = target
            return closed
        closing = min(abs(signed), abs(pos.quantity))
        frac = closing / abs(pos.quantity)
        entry_ch = pos.entry_charges * frac
        exit_ch = fill.charges * (closing / fill.quantity)
        gross = (fill.price - pos.avg_price) * closing * (1 if pos.quantity > 0 else -1)
        self.realized_gross += gross
        t = Trade(new_id("t_"), pos.position_id, pos.strategy, pos.instrument, pos.side, closing, pos.avg_price,
                  fill.price, pos.opened_at, fill.ts, round(gross, 2), round(entry_ch + exit_ch, 2), exit_reason,
                  round(pos.max_favorable * closing, 2), round(pos.max_adverse * closing, 2))
        self.trades.append(t)
        closed.append(t)
        pos.entry_charges -= entry_ch
        remaining = abs(signed) - closing
        pos.quantity += closing * fill.side.sign
        if pos.quantity == 0:
            del self.positions[fill.instrument]
        if remaining > 0:  # flip into the opposite direction
            self._open(fill, remaining * fill.side.sign, fill.charges - exit_ch, stop_loss, target, signal_id)
        return closed

    def _open(self, fill: Fill, signed: int, charges: float, sl, tgt, signal_id) -> None:
        self.positions[fill.instrument] = ManagedPosition(
            fill.instrument, fill.strategy, signed, fill.price, fill.ts, sl, tgt, fill.price, charges, signal_id)
        self.entries.append((fill.ts.date(), fill.strategy, fill.instrument))

    def set_bracket(self, instrument: str, stop_loss: float | None, target: float | None) -> None:
        pos = self.positions.get(instrument)
        if pos:
            pos.stop_loss, pos.target = stop_loss, target

    def mark(self, instrument: str, price: float) -> None:
        pos = self.positions.get(instrument)
        if pos is None:
            return
        pos.last_price = price
        move = (price - pos.avg_price) * (1 if pos.quantity > 0 else -1)
        pos.max_favorable = max(pos.max_favorable, move)
        pos.max_adverse = min(pos.max_adverse, move)

    # ---------------------------------------------------------------- views
    @property
    def unrealized(self) -> float:
        return sum(p.unrealized for p in self.positions.values())

    @property
    def realized_net(self) -> float:
        return self.realized_gross - sum(t.charges for t in self.trades)

    def equity(self) -> float:
        return self.capital + self.realized_gross + self.unrealized - self.total_charges

    def snapshot(self, ts: datetime) -> float:
        eq = self.equity()
        if self.equity_curve and self.equity_curve[-1][0] == ts:
            self.equity_curve[-1] = (ts, eq)
        else:
            self.equity_curve.append((ts, eq))
        self._peak = max(self._peak, eq)
        dd = self._peak - eq
        if dd > self.max_drawdown:
            self.max_drawdown = dd
            self.max_drawdown_pct = 100 * dd / self._peak if self._peak else 0.0
        return eq

    def trades_on(self, day: date, strategy: str | None = None) -> list[Trade]:
        return [t for t in self.trades if t.exit_ts.date() == day and (strategy is None or t.strategy == strategy)]

    def entries_on(self, day: date, strategy: str | None = None) -> int:
        return sum(1 for d, s, _ in self.entries if d == day and (strategy is None or s == strategy))

    def day_pnl(self, day: date, strategy: str | None = None) -> float:
        """Net realized P&L of trades closed today + unrealized P&L of open positions (minus their entry charges)."""
        realized = sum(t.net_pnl for t in self.trades_on(day, strategy))
        opens = [p for p in self.positions.values() if strategy is None or p.strategy == strategy]
        return realized + sum(p.unrealized - p.entry_charges for p in opens)

    def consecutive_losses(self, day: date | None = None, strategy: str | None = None) -> int:
        n = 0
        trades = self.trades if day is None else self.trades_on(day)
        for t in reversed(trades):
            if strategy is not None and t.strategy != strategy:
                continue
            if t.net_pnl < 0:
                n += 1
            else:
                break
        return n

    def drawdown_now(self) -> tuple[float, float]:
        eq = self.equity()
        dd = max(0.0, self._peak - eq)
        return dd, (100 * dd / self._peak if self._peak else 0.0)

    # ---------------------------------------------------------------- persistence
    def to_state(self, today: date) -> dict:
        return {
            "capital": self.capital, "realized_gross": self.realized_gross, "total_charges": self.total_charges,
            "peak": self._peak, "max_drawdown": self.max_drawdown, "max_drawdown_pct": self.max_drawdown_pct,
            "positions": [asdict(p) | {"opened_at": p.opened_at.isoformat()} for p in self.positions.values()],
            "entries": [(d.isoformat(), s, i) for d, s, i in self.entries if d == today],
            "trades_today": [asdict(t) | {"entry_ts": t.entry_ts.isoformat(), "exit_ts": t.exit_ts.isoformat()}
                             for t in self.trades_on(today)],
        }

    def load_state(self, st: dict) -> None:
        self.capital = st.get("capital", self.capital)
        self.realized_gross = st.get("realized_gross", 0.0)
        self.total_charges = st.get("total_charges", 0.0)
        self._peak = st.get("peak", self.capital)
        self.max_drawdown = st.get("max_drawdown", 0.0)
        self.max_drawdown_pct = st.get("max_drawdown_pct", 0.0)
        for p in st.get("positions", []):
            p = dict(p)
            p["opened_at"] = datetime.fromisoformat(p["opened_at"])
            mp = ManagedPosition(**p)
            self.positions[mp.instrument] = mp
        self.entries = [(date.fromisoformat(d), s, i) for d, s, i in st.get("entries", [])]
        for t in st.get("trades_today", []):
            t = dict(t)
            t["entry_ts"], t["exit_ts"] = datetime.fromisoformat(t["entry_ts"]), datetime.fromisoformat(t["exit_ts"])
            self.trades.append(Trade(**t))
