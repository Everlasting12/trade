"""Live market-data feeds. Feeds run on their own threads and only *enqueue* data; they never touch
engine state. The runtime's worker thread consumes the queue.

- KiteFeed: Kite WebSocket (KiteTicker) in FULL mode (LTP, OHLC, volume, OI, depth, exchange time).
  KiteTicker reconnects with exponential backoff; on reconnect we resubscribe. Order postbacks also
  arrive on this socket.
- SyntheticFeed: random-walk ticks on a virtual session clock, for testing paper trading outside
  market hours. Paper mode only.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

import numpy as np

from app.core.clock import IST, SESSION_OPEN, Clock, to_ist
from app.core.models import Tick

log = logging.getLogger(__name__)


def tick_from_kite(t: dict) -> Tick:
    depth = t.get("depth") or {}
    buy = (depth.get("buy") or [{}])[0]
    sell = (depth.get("sell") or [{}])[0]
    ohlc = t.get("ohlc") or {}
    ts = t.get("exchange_timestamp") or t.get("last_trade_time") or datetime.now(IST)
    return Tick(
        instrument_token=t["instrument_token"], ts=to_ist(ts), ltp=float(t.get("last_price") or 0),
        cumulative_volume=t.get("volume_traded"), oi=t.get("oi"),
        bid=buy.get("price") or None, ask=sell.get("price") or None,
        bid_qty=buy.get("quantity") or None, ask_qty=sell.get("quantity") or None,
        day_open=ohlc.get("open"), day_high=ohlc.get("high"), day_low=ohlc.get("low"), prev_close=ohlc.get("close"),
        vwap=t.get("average_traded_price"),
    )


class KiteFeed:
    def __init__(self, api_key: str, access_token: str, on_ticks: Callable[[list[Tick]], None],
                 on_order_update: Callable[[dict], None], on_status: Callable[[str], None] | None = None):
        from kiteconnect import KiteTicker
        from twisted.internet import reactor

        self._reactor = reactor
        self.on_ticks_cb = on_ticks
        self.on_order_update_cb = on_order_update
        self.on_status = on_status or (lambda s: None)
        self.tokens: set[int] = set()
        self.status = "disconnected"
        self.last_tick_at: datetime | None = None
        kws = KiteTicker(api_key, access_token)
        kws.on_ticks = self._on_ticks
        kws.on_connect = self._on_connect
        kws.on_close = lambda ws, code, reason: self._set(f"closed: {reason}")
        kws.on_error = lambda ws, code, reason: self._set(f"error: {reason}")
        kws.on_reconnect = lambda ws, n: self._set(f"reconnecting ({n})")
        kws.on_noreconnect = lambda ws: self._set("gave up reconnecting")
        kws.on_order_update = lambda ws, data: self.on_order_update_cb(data)
        self.kws = kws

    def _set(self, s: str) -> None:
        self.status = s
        self.on_status(s)

    def start(self) -> None:
        self._set("connecting")
        # Twisted's reactor can only be started once per process; KiteTicker starts it on first connect.
        if self._reactor.running:
            self._reactor.callFromThread(self.kws.connect, threaded=True)
        else:
            self.kws.connect(threaded=True)

    def stop(self) -> None:
        self.kws.stop_retry()
        if self._reactor.running:
            self._reactor.callFromThread(self.kws.close)
        self._set("stopped")

    def _on_connect(self, ws, response) -> None:
        self._set("connected")
        if self.tokens:
            ws.subscribe(list(self.tokens))
            ws.set_mode(ws.MODE_FULL, list(self.tokens))

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        self.last_tick_at = datetime.now(IST)
        out = []
        for t in ticks:
            try:
                out.append(tick_from_kite(t))
            except Exception:
                log.exception("bad tick %s", t)
        if out:
            self.on_ticks_cb(out)

    def subscribe(self, tokens: list[int]) -> None:
        new = [t for t in tokens if t not in self.tokens]
        self.tokens.update(tokens)
        if new and self.kws.is_connected():
            self._reactor.callFromThread(self.kws.subscribe, new)
            self._reactor.callFromThread(self.kws.set_mode, self.kws.MODE_FULL, new)

    def unsubscribe(self, tokens: list[int]) -> None:
        gone = [t for t in tokens if t in self.tokens]
        self.tokens.difference_update(gone)
        if gone and self.kws.is_connected():
            self._reactor.callFromThread(self.kws.unsubscribe, gone)


class ReplayClock(Clock):
    """Virtual session clock: starts at 09:15 of the given day and runs `speed`x real time."""

    def __init__(self, day, speed: float = 10.0, start_offset_minutes: float = 0.0):
        self.speed = speed
        self._v0 = datetime.combine(day, SESSION_OPEN, IST) + timedelta(minutes=start_offset_minutes)
        self._r0 = time.monotonic()

    def now(self) -> datetime:
        return self._v0 + timedelta(seconds=(time.monotonic() - self._r0) * self.speed)


class SyntheticFeed:
    def __init__(self, clock: Clock, instruments: dict[int, float], on_ticks: Callable[[list[Tick]], None],
                 interval_s: float = 0.25, annual_vol: float = 0.18, seed: int | None = None):
        self.clock = clock
        self.prices = dict(instruments)
        self.on_ticks_cb = on_ticks
        self.interval_s = interval_s
        self.status = "stopped"
        self.tokens = set(instruments)
        self.last_tick_at: datetime | None = None
        self._rng = np.random.default_rng(seed)
        self._vol = {t: 0 for t in instruments}
        self._drift = {t: 0.0 for t in instruments}
        self._annual_vol = annual_vol
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.status = "connected (synthetic)"
        self._thread = threading.Thread(target=self._run, name="synthetic-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.status = "stopped"

    def subscribe(self, tokens: list[int]) -> None:
        pass

    def unsubscribe(self, tokens: list[int]) -> None:
        pass

    def _run(self) -> None:
        last_v = self.clock.now()
        while not self._stop.is_set():
            time.sleep(self.interval_s)
            now = self.clock.now()
            dt_years = max((now - last_v).total_seconds(), 1e-3) / (252 * 22500)
            last_v = now
            sigma = self._annual_vol * np.sqrt(dt_years)
            ticks = []
            for tok, px in self.prices.items():
                if self._rng.random() < 0.01:
                    self._drift[tok] = self._rng.normal(0, sigma * 0.1)
                px *= float(np.exp(self._rng.normal(self._drift[tok], sigma)))
                self.prices[tok] = px
                self._vol[tok] += int(self._rng.integers(50, 1500))
                spread = max(0.05, round(px * 0.00002 / 0.05) * 0.05)
                ltp = round(round(px / 0.05) * 0.05, 2)
                ticks.append(Tick(tok, now, ltp, self._vol[tok], None, round(ltp - spread, 2), round(ltp + spread, 2),
                                  int(self._rng.integers(75, 3000)), int(self._rng.integers(75, 3000))))
            self.last_tick_at = datetime.now(IST)
            self.on_ticks_cb(ticks)
