"""Structured event logging.

Every important action is recorded as a JSON object with a fixed set of fields (timestamp, event,
level, strategy, instrument, order/position ids, reason, error, data). Events go to:
- a rotating JSON-lines file (logs/events.jsonl) for audit,
- an in-memory ring buffer for the dashboard,
- an optional sink (the database repository).
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.core.clock import Clock

_log = logging.getLogger("trading.events")


class EventLog:
    def __init__(self, clock: Clock | None = None, log_dir: str | Path | None = None, buffer: int = 2000,
                 mode: str = "", sink: Callable[[dict], None] | None = None, echo: bool = True):
        self.clock = clock or Clock()
        self.mode = mode
        self.buffer: deque[dict] = deque(maxlen=buffer)
        self.sink = sink
        self.echo = echo
        self._file_logger: logging.Logger | None = None
        if log_dir:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            fl = logging.getLogger(f"trading.events.file.{Path(log_dir).resolve()}")
            if not fl.handlers:
                h = logging.handlers.RotatingFileHandler(Path(log_dir) / "events.jsonl", maxBytes=20_000_000,
                                                         backupCount=10, encoding="utf-8")
                h.setFormatter(logging.Formatter("%(message)s"))
                fl.addHandler(h)
                fl.setLevel(logging.INFO)
                fl.propagate = False
            self._file_logger = fl

    def emit(self, event: str, level: str = "INFO", *, strategy: str | None = None, instrument: str | None = None,
             action: str | None = None, reason: str | None = None, order_id: str | None = None,
             position_id: str | None = None, error: str | None = None, **data: Any) -> dict:
        rec = {
            "ts": self.clock.now().isoformat(), "mode": self.mode, "level": level, "event": event,
            "strategy": strategy, "instrument": instrument, "action": action, "reason": reason,
            "order_id": order_id, "position_id": position_id, "error": error,
            "data": data or None,
        }
        self.buffer.append(rec)
        line = json.dumps(rec, default=_default)
        if self._file_logger:
            self._file_logger.info(line)
        if self.echo and level in ("WARNING", "ERROR", "CRITICAL"):
            _log.log(getattr(logging, level), line)
        if self.sink:
            try:
                self.sink(rec)
            except Exception:  # logging must never break trading
                _log.exception("event sink failed")
        return rec

    def recent(self, n: int = 200, level: str | None = None, event: str | None = None) -> list[dict]:
        out = [r for r in reversed(self.buffer)
               if (level is None or r["level"] == level) and (event is None or r["event"] == event)]
        return out[:n]


class NullEventLog(EventLog):
    """Backtests: keep the buffer (for debugging) but write nothing to disk."""

    def __init__(self, clock: Clock | None = None):
        super().__init__(clock=clock, log_dir=None, buffer=5000, mode="backtest", echo=False)


def _default(o: Any):
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "as_dict"):
        return o.as_dict()
    return str(o)
