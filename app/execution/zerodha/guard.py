"""Live-trading guard: independent layers that must ALL pass before a live order reaches Zerodha.

1. LIVE_TRADING_ENABLED=true in the environment (requires a deliberate config change + restart).
2. TRADING_MODE=live in the environment.
3. Startup reconciliation with the broker completed without unresolved mismatches.
4. An operator armed live trading at runtime by typing the exact confirmation phrase. Arming expires
   at the end of the trading day and is cleared by any restart or kill switch.
5. The global kill switch is off.
6. Per-order hard caps in the Zerodha adapter (max lots per order).

The engine checks `block_reason()` before submitting entries, and the Zerodha adapter checks it again
inside place_order() — so a bug in one layer cannot by itself send a live order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from app.config.settings import LIVE_CONFIRM_PHRASE, Settings
from app.core.models import TradingMode


@dataclass
class ArmState:
    armed: bool = False
    armed_at: datetime | None = None
    armed_by: str = ""
    day: date | None = None


class LiveTradingGuard:
    def __init__(self, settings: Settings, now: Callable[[], datetime], kill_switch_active: Callable[[], bool]):
        self.settings = settings
        self._now = now
        self._kill = kill_switch_active
        self.state = ArmState()
        self.reconciled = False
        self.reconcile_issues: list[str] = []

    def block_reason(self) -> str | None:
        s = self.settings
        if not s.live_trading_enabled:
            return "LIVE_TRADING_ENABLED is false"
        if s.trading_mode != TradingMode.LIVE:
            return "TRADING_MODE is not live"
        if not self.reconciled:
            return "startup reconciliation with broker not completed"
        if self.reconcile_issues:
            return "unresolved reconciliation mismatches: " + "; ".join(self.reconcile_issues)
        if self._kill():
            return "kill switch active"
        st = self.state
        if not st.armed:
            return "live trading not armed by operator"
        if st.day != self._now().date():
            self.disarm()
            return "arming expired (new trading day)"
        return None

    def arm(self, phrase: str, operator: str = "operator") -> None:
        if phrase.strip() != LIVE_CONFIRM_PHRASE:
            raise ValueError("Confirmation phrase does not match")
        self.state = ArmState(False)  # evaluate other layers with arming off
        pre = self._precheck()
        if pre:
            raise PermissionError(pre)
        now = self._now()
        self.state = ArmState(True, now, operator, now.date())

    def _precheck(self) -> str | None:
        reason = self.block_reason()
        return None if reason == "live trading not armed by operator" else reason

    def disarm(self) -> None:
        self.state = ArmState()

    def status(self) -> dict:
        st = self.state
        return {"armed": st.armed, "armed_at": st.armed_at.isoformat() if st.armed_at else None,
                "armed_by": st.armed_by, "block_reason": self.block_reason(), "reconciled": self.reconciled,
                "reconcile_issues": self.reconcile_issues, "confirm_phrase": LIVE_CONFIRM_PHRASE}
