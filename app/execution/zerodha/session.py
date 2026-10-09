"""Kite Connect authentication/session (OAuth-style login flow, daily access token).

Never asks for the user's password and never stores the api_secret anywhere but the environment.
The access token is cached in a local file so a backend restart the same day doesn't require login.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from kiteconnect import KiteConnect
from kiteconnect.exceptions import KiteException

log = logging.getLogger(__name__)


class KiteSession:
    def __init__(self, api_key: str, api_secret: str, token_file: Path, access_token: str = ""):
        self.api_key = api_key
        self._api_secret = api_secret
        self.token_file = token_file
        self.kite: KiteConnect | None = KiteConnect(api_key=api_key) if api_key else None
        self.access_token: str | None = None
        self.profile: dict | None = None
        self.listeners: list = []      # callables(session) notified on login/logout
        if access_token:
            self._try(access_token)
        else:
            self._restore()

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self._api_secret)

    @property
    def logged_in(self) -> bool:
        return self.access_token is not None

    def login_url(self) -> str | None:
        return self.kite.login_url() if self.kite else None

    def complete_login(self, request_token: str) -> dict:
        if not self.configured:
            raise RuntimeError("BROKER_API_KEY / BROKER_API_SECRET not configured")
        data = self.kite.generate_session(request_token, api_secret=self._api_secret)
        self._activate(data["access_token"])
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({"access_token": data["access_token"],
                                               "date": date.today().isoformat(),
                                               "login_time": datetime.now().isoformat()}))
        self._notify()
        return self.profile

    def logout(self) -> None:
        if self.access_token and self.kite:
            try:
                self.kite.invalidate_access_token()
            except KiteException as e:
                log.warning("invalidate_access_token failed: %s", e)
        self.access_token = None
        self.profile = None
        self.kite = KiteConnect(api_key=self.api_key) if self.api_key else None
        self.token_file.unlink(missing_ok=True)
        self._notify()

    def _restore(self) -> None:
        if not self.token_file.exists() or not self.kite:
            return
        try:
            saved = json.loads(self.token_file.read_text())
            self._try(saved["access_token"])
        except Exception as e:
            log.info("Saved Kite session not usable: %s", e)
            self.token_file.unlink(missing_ok=True)

    def _try(self, token: str) -> None:
        try:
            self._activate(token)
            log.info("Kite session active for %s", self.profile.get("user_id"))
        except Exception as e:
            log.info("Kite token invalid/expired (%s); login required", e)
            self.access_token = None
            self.profile = None
            self.kite = KiteConnect(api_key=self.api_key) if self.api_key else None

    def _activate(self, token: str) -> None:
        self.kite.set_access_token(token)
        self.profile = self.kite.profile()     # validates the token
        self.access_token = token

    def _notify(self) -> None:
        for fn in list(self.listeners):
            try:
                fn(self)
            except Exception:
                log.exception("session listener failed")
