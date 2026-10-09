"""Runs the TradingEngine for paper/live trading.

Concurrency model (why the UI never blocks and state is never corrupted)
- Exactly ONE worker thread owns the engine. Everything that touches engine state runs on it:
  ticks, order postbacks, the 1-second timer, and operator commands.
- Feed threads (KiteTicker/Twisted, synthetic) only put items on a queue.
- API threads submit commands as callables and wait on a Future; reads use an immutable snapshot
  that the worker republishes every second (atomic reference swap, no locks on the hot path).
- Streamlit is just an HTTP client of the API: its reruns/sessions can't affect trading.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from typing import Any, Callable

from app.config import factory
from app.config.settings import Settings, load_trading_config
from app.core.clock import Clock, IST
from app.core.models import Instrument, Signal, SignalAction, Tick, TradingMode
from app.database.repository import Repository
from app.engine.engine import EngineConfig, TradingEngine
from app.execution.paper.broker import PaperBroker
from app.market_data.historical.service import HistoricalDataService
from app.market_data.instruments import InstrumentRegistry
from app.market_data.live.feeds import KiteFeed, ReplayClock, SyntheticFeed
from app.options.market import KiteOptionMarket, ModelOptionMarket
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskManager
from app.signals.scoring import SignalScorer
from app.utils.events import EventLog

log = logging.getLogger(__name__)


def last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def synthetic_registry(cfg: dict) -> tuple[InstrumentRegistry, dict[int, float]]:
    reg = InstrumentRegistry()
    prices = {}
    expiry = date.today() + timedelta(days=30)
    for i, s in enumerate((cfg.get("synthetic") or {}).get("instruments", [])):
        u = s["underlying"].upper()
        inst = Instrument(900_000 + i, "NFO", f"{u}-SYNTH-FUT", "FUT", "NFO-FUT", u, int(s["lot_size"]), 0.05, expiry)
        reg.add(inst)
        prices[inst.token] = float(s["price"])
    return reg, prices


WATCH_TIMEFRAMES = ("1m", "3m", "5m", "15m", "1h")


class LoginRequired(RuntimeError):
    pass


class EngineRuntime:
    def __init__(self, settings: Settings, session: Any, repo: Repository, instruments: Callable[[], InstrumentRegistry]):
        self.settings = settings
        self.session = session
        self.repo = repo
        self._instruments = instruments
        self.status = "stopped"
        self.error: str | None = None
        self.engine: TradingEngine | None = None
        self.feed = None
        self.guard = None
        self.events: EventLog | None = None
        self.clock: Clock = Clock()
        self.latest_ticks: dict[int, dict] = {}
        self.watch_tokens: set[int] = set()
        self._q: queue.Queue = queue.Queue(maxsize=200_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict = {"status": self.status}
        self._last_persist = 0.0
        self._last_poll = 0.0
        self._last_reconcile = 0.0

    # ================================================================ lifecycle
    @property
    def mode(self) -> TradingMode:
        return self.settings.trading_mode

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self._build()
        except LoginRequired as e:
            self.status, self.error = "waiting_for_login", str(e)
            self._snapshot = {"status": self.status, "error": self.error, "mode": str(self.mode)}
            return
        except Exception as e:
            log.exception("engine start failed")
            self.status, self.error = "error", str(e)
            self._snapshot = {"status": self.status, "error": self.error, "mode": str(self.mode)}
            return
        self._thread = threading.Thread(target=self._run, name="engine-worker", daemon=True)
        self._thread.start()
        self.status, self.error = "running", None

    def stop(self) -> None:
        self._stop.set()
        if self.feed:
            try:
                self.feed.stop()
            except Exception:
                log.exception("feed stop failed")
        if self._thread:
            self._thread.join(timeout=5)
        if self.engine:
            self._persist(force=True)
        self.status = "stopped"
        self._snapshot = {**self._snapshot, "status": "stopped"}

    def restart(self) -> None:
        self.stop()
        self.engine = self.feed = self.guard = None
        self._q = queue.Queue(maxsize=200_000)
        self.start()

    # ================================================================ build
    def _build(self) -> None:
        s = self.settings
        synthetic = s.feed_source == "synthetic"
        if s.trading_mode == TradingMode.LIVE:
            if not s.live_trading_enabled:
                raise RuntimeError("TRADING_MODE=live but LIVE_TRADING_ENABLED is not true. Refusing to start.")
            if synthetic:
                raise RuntimeError("FEED_SOURCE=synthetic is not allowed in live mode.")
        if not synthetic and not (self.session and self.session.logged_in):
            raise LoginRequired("Log in with Kite to start the engine (or set FEED_SOURCE=synthetic for a simulated session).")

        cfg = load_trading_config(s.trading_config)
        self.clock = ReplayClock(last_weekday(date.today()), s.synthetic_speed) if synthetic else Clock()
        mode = s.trading_mode
        self.events = EventLog(self.clock, s.log_dir, mode=str(mode), sink=self.repo.save_event)

        prices: dict[int, float] = {}
        if synthetic:
            registry, prices = synthetic_registry(cfg)
        else:
            registry = self._instruments()
        strategies, skipped = [], []
        for st in factory.strategies(cfg, s.default_timeframe):
            try:
                for spec in st.instruments:
                    registry.resolve(spec)
                strategies.append(st)
            except KeyError as ex:
                skipped.append(f"{st.id}: {ex}")

        risk = RiskManager(factory.risk_config(cfg, s))
        default_scoring, overrides = factory.scoring_configs(cfg)
        portfolio = Portfolio(s.default_capital)
        paper_cfg = factory.paper_config(cfg, s.default_capital)

        if mode == TradingMode.LIVE:
            from app.execution.zerodha.broker import ZerodhaBroker
            from app.execution.zerodha.guard import LiveTradingGuard
            self.guard = LiveTradingGuard(s, self.clock.now, lambda: risk.kill_switch.active)
            approx = PaperBroker(registry.get, paper_cfg, clock=self.clock)
            broker = ZerodhaBroker(self.session.kite, self.guard, registry.get, self.clock, s.live_max_order_lots,
                                   fallback_margin=approx.estimate_margin)
        else:
            broker = PaperBroker(registry.get, paper_cfg, clock=self.clock)

        engine_cfg = cfg.get("engine") or {}
        options_cfg, options_overrides = factory.options_configs(cfg)
        holder: dict = {}
        if synthetic:
            syn = cfg.get("synthetic") or {}
            option_market = ModelOptionMarket(
                registry, lambda name: holder["engine"].underlying_price(name), self.clock,
                iv=float(syn.get("option_iv", 0.14)),
                strike_steps={i["underlying"].upper(): i["strike_step"] for i in syn.get("instruments", [])
                              if i.get("strike_step")})
        else:
            option_market = KiteOptionMarket(self.session.kite, registry, self.clock)
            if isinstance(broker, PaperBroker):
                broker.margin_override = self._kite_margin
        self.engine = engine = TradingEngine(
            mode=mode, broker=broker, registry=registry, strategies=strategies, risk=risk,
            scorer=SignalScorer(default_scoring, overrides), portfolio=portfolio, events=self.events,
            clock=self.clock, repo=self.repo,
            config=EngineConfig(base_timeframe=engine_cfg.get("base_timeframe", "1m"),
                                default_product=factory.default_product(cfg),
                                expiry_day_exit_time=engine_cfg.get("expiry_day_exit_time", "15:00"),
                                option_stop_buffer_pct=float(engine_cfg.get("option_stop_buffer_pct", 5.0))),
            option_market=option_market, options_config=options_cfg, options_overrides=options_overrides)
        holder["engine"] = engine
        engine.on_instrument_added = self._subscribe_instrument
        if self.guard:
            engine.entry_gate = self.guard.block_reason

        self._restore_state(synthetic)
        # Contracts with open positions/orders from before a restart must be streamed again.
        for key in set(engine.portfolio.positions) | {o.instrument for o in engine.oms.working()}:
            if key not in engine.aggregators and key in registry:
                engine.ensure_instrument(registry.get(key))
        if synthetic:
            prices = self._synthetic_warmup(prices, int(engine_cfg.get("warmup_days", 5)))
        else:
            self._warmup(int(engine_cfg.get("warmup_days", 5)))

        # Watchlist instruments: stream them and build candles from their ticks.
        for tok in set(self.repo.all_watchlist_tokens()) | self.watch_tokens:
            inst = registry.by_token(tok)
            if inst is not None:
                engine.ensure_instrument(inst, timeframes=WATCH_TIMEFRAMES)
        tokens = [i.token for i in engine.instruments]
        if synthetic:
            self.feed = SyntheticFeed(self.clock, {t: prices[t] for t in tokens}, self._enqueue_ticks)
        else:
            self.feed = KiteFeed(self.session.api_key, self.session.access_token, self._enqueue_ticks,
                                 lambda d: self._q.put(("postback", d)),
                                 lambda st: self.events.emit("feed_status", "WARNING" if "error" in st or "closed" in st
                                                             else "INFO", reason=st))
            self.feed.subscribe(tokens + list(self.watch_tokens))
        self.feed.start()
        if mode == TradingMode.LIVE:
            self._reconcile_live(startup=True)
        for msg in skipped:
            self.events.emit("strategy_skipped", "WARNING", reason=f"instrument not resolvable - {msg}")
        self.events.emit("engine_started", reason=f"mode={mode} feed={s.feed_source}",
                         strategies=[st.id for st in strategies], instruments=[i.key for i in engine.instruments])

    def _subscribe_instrument(self, inst) -> None:
        if self.feed is not None:
            self.feed.subscribe([inst.token])

    _margin_cache: dict = {}

    def _kite_margin(self, order, inst, price):
        """Exact SPAN+exposure margin from Kite for paper trades (cached 60 s). None -> approximation."""
        if not inst.is_derivative or not (self.session and self.session.logged_in):
            return None
        key = (inst.key, str(order.side), order.quantity)
        hit = self._margin_cache.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        res = self.session.kite.order_margins([{
            "exchange": inst.exchange, "tradingsymbol": inst.tradingsymbol, "transaction_type": str(order.side),
            "variety": "regular", "product": str(order.product), "order_type": "MARKET",
            "quantity": order.quantity, "price": 0, "trigger_price": 0}])
        val = float(res[0]["total"]) if res else None
        self._margin_cache[key] = (time.monotonic(), val)
        return val

    def _warmup(self, days: int) -> None:
        if days <= 0:
            return
        hist = HistoricalDataService(self.session.kite, self.repo)
        now = datetime.now(IST)
        for inst in self.engine.underlyings:
            try:
                candles = hist.fetch(inst.token, self.engine.cfg.base_timeframe, now - timedelta(days=days + 3), now)
                n = self.engine.warmup(inst.key, candles)
                self.events.emit("warmup_complete", instrument=inst.key, candles=n)
            except Exception as e:
                self.events.emit("warmup_failed", "WARNING", instrument=inst.key, error=str(e),
                                 reason="historical data add-on may not be enabled; indicators warm up from live ticks")

    def _synthetic_warmup(self, prices: dict[int, float], days: int) -> dict[int, float]:
        """Synthetic history for the days before the virtual session, so indicators are warm at 09:15."""
        from app.market_data.historical.service import synthetic_candles
        session_day = self.clock.now().date()
        start = session_day - timedelta(days=days * 2 + 2)
        out = dict(prices)
        for inst in self.engine.underlyings:
            hist = [c for c in synthetic_candles(start, days * 2, self.engine.cfg.base_timeframe,
                                                 prices[inst.token], seed=inst.token) if c.ts.date() < session_day]
            n = self.engine.warmup(inst.key, hist[-days * 375:])
            if hist:
                out[inst.token] = hist[-1].close
            self.events.emit("warmup_complete", instrument=inst.key, candles=n, reason="synthetic history")
        return out

    # ================================================================ state persistence / recovery
    def _persist(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_persist < 5:
            return
        self._last_persist = time.monotonic()
        e = self.engine
        today = self.clock.now().date()
        try:
            self.repo.save_state("portfolio", e.portfolio.to_state(today))
            self.repo.save_state("oms", e.oms.to_state())
            self.repo.save_state("risk", e.risk_state())
            self.repo.save_state("strategy_enabled", {sid: s.enabled for sid, s in e.strategies.items()})
            self.repo.save_state("auto_trading", {"on": e.auto_trading})
            if isinstance(e.broker, PaperBroker):
                self.repo.save_state("paper_broker", e.broker.to_state())
        except Exception:
            log.exception("state persist failed")

    def _restore_state(self, synthetic: bool) -> None:
        e = self.engine
        if synthetic:
            return  # a synthetic session always starts clean
        e.load_risk_state(self.repo.load_state("risk"))
        at = self.repo.load_state("auto_trading")
        if at is not None:
            e.auto_trading = bool(at.get("on", True))
        for sid, enabled in (self.repo.load_state("strategy_enabled") or {}).items():
            if sid in e.strategies:
                e.strategies[sid].enabled = enabled
                e.stats[sid]["status"] = "enabled" if enabled else "disabled"
        pf = self.repo.load_state("portfolio")
        if pf:
            e.portfolio.load_state(pf)
        oms = self.repo.load_state("oms")
        if oms:
            e.oms.load_state(oms)
        if isinstance(e.broker, PaperBroker):
            pb = self.repo.load_state("paper_broker")
            if pb:
                e.broker.load_state(pb)
        if pf or oms:
            self.events.emit("state_restored", "WARNING", positions=len(e.portfolio.positions),
                             working_orders=len(e.oms.working()))

    def _reconcile_live(self, startup: bool = False) -> list[str]:
        e = self.engine
        issues: list[str] = []
        try:
            e.oms.reconcile()
            broker_pos = {p.instrument: p.quantity for p in e.broker.get_positions() if p.quantity}
            ours = {k: p.quantity for k, p in e.portfolio.positions.items()}
            for k in set(broker_pos) | set(ours):
                if broker_pos.get(k, 0) != ours.get(k, 0):
                    issues.append(f"{k}: broker {broker_pos.get(k, 0)} vs internal {ours.get(k, 0)}")
            if self.guard:
                self.guard.reconciled = True
                self.guard.reconcile_issues = issues
            self.events.emit("reconciliation", "CRITICAL" if issues else "INFO",
                             reason="; ".join(issues) or "broker and internal state match", startup=startup)
        except Exception as ex:
            if self.guard and startup:
                self.guard.reconciled = False
            self.events.emit("reconciliation_failed", "ERROR", error=str(ex))
        return issues

    # ================================================================ worker
    def _enqueue_ticks(self, ticks: list[Tick]) -> None:
        try:
            self._q.put_nowait(("ticks", ticks))
        except queue.Full:
            log.error("engine queue full; dropping ticks")

    def submit(self, fn: Callable[[TradingEngine], Any]) -> Future:
        fut: Future = Future()
        if not self.engine or not self._thread or not self._thread.is_alive():
            fut.set_exception(RuntimeError(f"engine not running ({self.status}: {self.error or ''})"))
            return fut
        self._q.put(("call", fn, fut))
        return fut

    def call(self, fn: Callable[[TradingEngine], Any], timeout: float = 15.0) -> Any:
        return self.submit(fn).result(timeout=timeout)

    def _run(self) -> None:
        last_timer = 0.0
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self._handle(item)
                if time.monotonic() - last_timer >= 1.0:
                    last_timer = time.monotonic()
                    self._on_timer()
            except Exception as e:  # never let the worker die silently
                log.exception("engine worker error")
                if self.events:
                    self.events.emit("engine_error", "ERROR", error=repr(e))

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        e = self.engine
        if kind == "ticks":
            for t in item[1]:
                inst = e.registry.by_token(t.instrument_token)
                self.latest_ticks[t.instrument_token] = {
                    "token": t.instrument_token, "instrument": inst.key if inst else None, "ts": t.ts.isoformat(), "ltp": t.ltp, "volume": t.cumulative_volume,
                    "oi": t.oi, "bid": t.bid, "ask": t.ask, "open": t.day_open, "high": t.day_high,
                    "low": t.day_low, "prev_close": t.prev_close, "vwap": t.vwap}
                e.on_tick(t)
        elif kind == "postback":
            if hasattr(e.broker, "on_postback"):
                e.broker.on_postback(item[1])
        elif kind == "call":
            fn, fut = item[1], item[2]
            if not fut.set_running_or_notify_cancel():
                return
            try:
                result, error = fn(e), None
            except Exception as ex:
                result, error = None, ex
            # Persist + republish before replying, so the caller's next read sees the new state.
            self._persist(force=True)
            self._publish()
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(result)

    def _on_timer(self) -> None:
        e = self.engine
        e.on_timer()
        now = time.monotonic()
        if e.mode == TradingMode.LIVE:
            if now - self._last_poll >= 5:
                self._last_poll = now
                try:
                    e.broker.poll()
                except Exception as ex:
                    self.events.emit("broker_api_error", "ERROR", error=str(ex), reason="order poll failed")
            if now - self._last_reconcile >= 60:
                self._last_reconcile = now
                issues = self._reconcile_live()
                if issues and self.guard and self.guard.state.armed:
                    self.guard.disarm()
                    self.events.emit("live_disarmed", "CRITICAL", reason="position mismatch with broker")
        self._persist()
        self._publish()

    # ================================================================ snapshot
    def _publish(self) -> None:
        e = self.engine
        try:
            snap = {
                "status": self.status, "error": self.error, "mode": str(e.mode),
                "feed": {"source": self.settings.feed_source, "status": getattr(self.feed, "status", None),
                         "last_tick_at": getattr(self.feed, "last_tick_at", None)},
                "clock": self.clock.now().isoformat(),
                "auto_trading": e.auto_trading,
                "underlyings": [{"key": i.key, "name": i.name, "token": i.token, "last_price": e.last_price.get(i.key)}
                                for i in e.underlyings],
                "overview": e.overview(),
                "positions": e.positions_view(),
                "orders": e.orders_view()[:300],
                "signals": list(e.signals)[:300],
                "strategies": e.strategies_view(),
                "market": e.market_status(),
                "equity_curve": [(ts.isoformat(), round(v, 2)) for ts, v in e.portfolio.equity_curve[-500:]],
                "trades_today": [t.as_dict() for t in e.portfolio.trades_on(self.clock.now().date())],
                "risk_config": vars(e.risk.cfg) | {"strategy_limits": {k: vars(v) for k, v in e.risk.cfg.strategy_limits.items()}},
                "live_guard": self.guard.status() if self.guard else None,
                "events": self.events.recent(300),
                "ticks": {str(k): v for k, v in self.latest_ticks.items()},
                "published_at": datetime.now(IST).isoformat(),
            }
            now = self.clock.now()
            curve = e.portfolio.equity_curve
            if not curve or (now - curve[-1][0]).total_seconds() >= 30:
                e.portfolio.snapshot(now)
            self._snapshot = snap
        except Exception:
            log.exception("snapshot failed")

    def snapshot(self) -> dict:
        snap = self._snapshot
        if self.status != "running":
            return {**snap, "status": self.status, "error": self.error, "mode": str(self.mode)}
        return snap

    # ================================================================ commands (run on the worker thread)
    def kill_switch(self, reason: str, flatten: bool = True) -> None:
        def fn(e: TradingEngine):
            e.kill_switch(reason, flatten)
            if self.guard:
                self.guard.disarm()
        self.call(fn)

    def set_auto_trading(self, on: bool) -> None:
        def fn(e: TradingEngine):
            e.auto_trading = on
            e.events.emit("auto_trading_on" if on else "auto_trading_off", "WARNING",
                          reason="new entries enabled" if on else "new entries paused; exits still managed")
        self.call(fn)

    def option_chain(self, underlying: str, expiry: str | None = None, width: int = 12) -> dict:
        """Chain from the engine's option market (Kite quotes, or the model in synthetic mode)."""
        def fn(e: TradingEngine):
            if e.option_market is None:
                raise RuntimeError("no option market")
            today = e.clock.now().date()
            exps = e.option_market.expiries(underlying, today)
            if not exps:
                raise KeyError(f"No option expiries for {underlying}")
            exp = date.fromisoformat(expiry) if expiry else exps[0]
            around = e.underlying_price(underlying)
            if around is None:
                raise KeyError(f"No live price for {underlying}; it must be an engine underlying")
            snap = e.option_market.chain(underlying, exp, around, width)
            return {"underlying": underlying, "expiry": str(exp), "expiries": [str(x) for x in exps],
                    "forward": round(snap.forward, 2), "atm_strike": snap.atm_strike,
                    "atm_iv": snap.atm_iv, "dte": round(snap.days_to_expiry(), 2), "source": snap.source,
                    "quotes": [q.as_dict() for q in snap.quotes]}
        return self.call(fn, timeout=30)

    def reset_kill_switch(self) -> None:
        self.call(lambda e: e.reset_kill_switch())

    def set_strategy_enabled(self, sid: str, enabled: bool) -> None:
        self.call(lambda e: e.set_strategy_enabled(sid, enabled))

    def exit_position(self, instrument: str, reason: str = "manual exit") -> dict | None:
        def fn(e: TradingEngine):
            o = e.oms.exit_position(instrument, reason)
            return o.as_dict() if o else None
        return self.call(fn)

    def manual_signal(self, body: dict) -> dict:
        """Operator trade ticket. Goes through the risk manager and OMS like any strategy signal
        (scoring is skipped: the operator is the strategy)."""
        def fn(e: TradingEngine):
            key = body["instrument"]
            inst = e.registry.get(key)
            sig = Signal(SignalAction(body["action"]), key, "manual", body.get("timeframe", "manual"),
                         body.get("entry_price") or e.last_price.get(key), body.get("stop_loss"), body.get("target"),
                         body.get("quantity"), 1.0, [body.get("reason") or "manual order"], e.clock.now())
            st = e.stats.setdefault("manual", {"signals": 0, "ignored_low_score": 0, "risk_rejected": 0,
                                               "approved": 0, "errors": 0, "consecutive_errors": 0, "status": "manual"})
            st["signals"] += 1
            if inst.key not in e.aggregators:
                return {"approved": False, "reasons": [f"{key} is not streamed by the engine (no live price)"]}
            decision = e._evaluate_risk(sig, inst)
            rec = {**sig.as_dict(), "grade": "manual", "risk": decision.as_dict(),
                   "status": "approved" if decision.approved else "risk_rejected"}
            e._record_signal(rec)
            if not decision.approved:
                st["risk_rejected"] += 1
                e.events.emit("risk_check_rejected", "WARNING", strategy="manual", instrument=key,
                              reason="; ".join(decision.reasons))
                return decision.as_dict()
            st["approved"] += 1
            if sig.action == SignalAction.EXIT:
                e.oms.exit_position(key, "manual exit")
            else:
                from app.core.models import Product
                e.oms.submit_entry(sig, decision.quantity, Product(body.get("product") or e.cfg.default_product))
            return decision.as_dict()
        return self.call(fn)

    def arm_live(self, phrase: str, operator: str) -> dict:
        if not self.guard:
            raise PermissionError("Not in live mode")
        def fn(e: TradingEngine):
            self.guard.arm(phrase, operator)
            e.events.emit("live_trading_armed", "CRITICAL", reason=f"armed by {operator}")
            return self.guard.status()
        return self.call(fn)

    def disarm_live(self) -> None:
        if self.guard:
            def fn(e: TradingEngine):
                self.guard.disarm()
                e.events.emit("live_trading_disarmed", "WARNING")
            self.call(fn)

    def acknowledge_reconciliation(self, operator: str) -> None:
        if not self.guard:
            return
        def fn(e: TradingEngine):
            e.events.emit("reconciliation_acknowledged", "CRITICAL", reason=f"by {operator}",
                          issues=self.guard.reconcile_issues)
            self.guard.reconcile_issues = []
        self.call(fn)

    def candles(self, instrument: str, tf: str, n: int = 300) -> list[dict]:
        def fn(e: TradingEngine):
            out = [c.as_dict() for c in e.store.series(instrument, tf).last(n)]
            forming = e.aggregators.get(instrument).forming(tf) if instrument in e.aggregators else None
            if forming:
                out.append({**forming.as_dict(), "forming": True})
            return out
        return self.call(fn)

    def watch(self, tokens: list[int]) -> None:
        """Stream instruments (watchlists): live ticks + candles built from them."""
        self.watch_tokens.update(tokens)
        if self.engine and self._thread and self._thread.is_alive():
            def fn(e: TradingEngine):
                for t in tokens:
                    inst = e.registry.by_token(t)
                    if inst is not None:
                        e.ensure_instrument(inst, timeframes=WATCH_TIMEFRAMES)   # also subscribes the feed
            self.call(fn)
        elif self.feed:
            self.feed.subscribe(tokens)

    def unwatch(self, tokens: list[int]) -> None:
        engine_tokens = {i.token for i in self.engine.instruments} if self.engine else set()
        drop = [t for t in tokens if t not in engine_tokens]
        self.watch_tokens.difference_update(drop)
        for t in drop:
            self.latest_ticks.pop(t, None)
        if self.feed:
            self.feed.unsubscribe(drop)
