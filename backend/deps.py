"""Process-wide singletons for the API: settings, Kite session, repository, instruments, engine runtime."""

from __future__ import annotations

import logging
import threading
from datetime import date

from fastapi import HTTPException

from app.config.settings import ROOT, Settings
from app.database.repository import Repository
from app.engine.runtime import EngineRuntime
from app.execution.zerodha.session import KiteSession
from app.market_data.instruments import InstrumentRegistry

log = logging.getLogger(__name__)


class InstrumentService:
    """Kite instrument master, loaded once per day (NSE/BSE incl. indices, NFO, BFO)."""

    EXCHANGES = ("NSE", "BSE", "NFO", "BFO")

    def __init__(self, session: KiteSession):
        self.session = session
        self._reg: InstrumentRegistry | None = None
        self._lock = threading.Lock()

    def registry(self) -> InstrumentRegistry:
        with self._lock:
            if self._reg is not None and self._reg.loaded_on == date.today():
                return self._reg
            if not self.session.logged_in:
                raise HTTPException(401, "Log in with Kite to load instruments")
            reg = InstrumentRegistry()
            for ex in self.EXCHANGES:
                try:
                    n = reg.load_kite_dump(self.session.kite.instruments(ex))
                    log.info("Loaded %d %s instruments", n, ex)
                except Exception as e:
                    log.warning("Instrument dump for %s failed: %s", ex, e)
            reg.loaded_on = date.today()
            self._reg = reg
            return reg

    def invalidate(self) -> None:
        with self._lock:
            self._reg = None


class Container:
    def __init__(self):
        self.settings = Settings.load()
        s = self.settings
        self.session = KiteSession(s.broker_api_key, s.broker_api_secret, ROOT / ".kite_session.json", s.access_token)
        self.repo = Repository(s.db_path, str(s.trading_mode))
        self.instruments = InstrumentService(self.session)
        self.runtime = EngineRuntime(s, self.session, self.repo, self.instruments.registry)
        self.oi_baseline: dict[tuple[date, int], float] = {}
        self.session.listeners.append(self._on_session_change)

    def _on_session_change(self, session: KiteSession) -> None:
        self.instruments.invalidate()
        if self.settings.feed_source == "kite":
            threading.Thread(target=self.runtime.restart, name="engine-restart", daemon=True).start()


_container: Container | None = None


def container() -> Container:
    global _container
    if _container is None:
        _container = Container()
    return _container


def kite():
    c = container()
    if not c.session.logged_in:
        raise HTTPException(401, "Not logged in to Kite")
    return c.session.kite


def runtime() -> EngineRuntime:
    return container().runtime
