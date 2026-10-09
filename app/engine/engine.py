"""The trading engine: one pipeline for backtest, paper and live.

    market data -> candles -> indicators -> strategy -> signal -> scorer -> risk -> OMS -> broker
                                                                                      |
                                                    portfolio <- fills <--------------+

The engine is single-threaded by design: in paper/live it is driven by one worker thread
(see runtime.py) that serialises ticks, timer events and operator commands, so none of the
components below need locks. Only the execution adapter differs between modes.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Iterable

from app.core.clock import SESSION_CLOSE, Clock, SimClock, parse_hhmm, to_ist
from app.core.models import (Candle, Instrument, Order, OrderRole, OrderType, Product, Signal, SignalAction, Tick,
                             TradingMode)
from app.execution.base import BrokerError, BrokerInterface
from app.execution.paper.broker import PaperBroker
from app.indicators.engine import IndicatorEngine
from app.options.market import OptionMarket
from app.options.planner import OptionsConfig, OptionsPlanner
from app.market_data import timeframes as tfm
from app.market_data.candles import CandleStore, MultiTimeframeAggregator
from app.market_data.instruments import InstrumentRegistry
from app.orders.manager import OrderManager
from app.portfolio import metrics
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskDecision, RiskInputs, RiskManager
from app.signals.scoring import SignalScorer
from app.strategies.base import PositionView, Strategy, StrategyContext
from app.utils.events import EventLog


@dataclass
class EngineConfig:
    base_timeframe: str = "1m"
    default_product: Product = Product.MIS
    candle_grace_seconds: float = 2.0
    enforce_session_times: bool = True
    max_strategy_errors: int = 5
    signal_history: int = 1000
    expiry_day_exit_time: str = "15:00"   # option positions expiring today are closed at this time
    option_stop_buffer_pct: float = 5.0   # stop-limit buffer for option stops (SL-M not allowed on options)


class NullRepo:
    def __getattr__(self, name):
        return lambda *a, **k: None


class TradingEngine:
    def __init__(self, *, mode: TradingMode, broker: BrokerInterface, registry: InstrumentRegistry,
                 strategies: Iterable[Strategy], risk: RiskManager, scorer: SignalScorer, portfolio: Portfolio,
                 events: EventLog, clock: Clock, config: EngineConfig | None = None, repo: Any = None,
                 option_market: OptionMarket | None = None, options_config: OptionsConfig | None = None,
                 options_overrides: dict[str, dict] | None = None):
        self.mode = mode
        self.broker = broker
        self.registry = registry
        self.risk = risk
        self.scorer = scorer
        self.portfolio = portfolio
        self.events = events
        self.clock = clock
        self.cfg = config or EngineConfig()
        self.repo = repo or NullRepo()
        self.store = CandleStore()
        self.aggregators: dict[str, MultiTimeframeAggregator] = {}
        self.indicators: dict[str, IndicatorEngine] = {}
        self.strategies: dict[str, Strategy] = {}
        self._subs: dict[tuple[str, str], list[str]] = {}       # (instrument, tf) -> strategy ids
        self.strategy_instruments: dict[str, list[str]] = {}    # strategy id -> resolved instrument keys
        self.signals: deque[dict] = deque(maxlen=self.cfg.signal_history)
        self.stats: dict[str, dict] = {}
        self.last_price: dict[str, float] = {}
        self.last_data_at: dict[str, datetime] = {}      # engine-clock time of last data
        self._last_data_mono: dict[str, float] = {}      # wall-clock (monotonic) time, for feed liveness
        self.entry_gate: Callable[[], str | None] = lambda: None   # live-trading guard hook
        self.on_instrument_added: Callable[[Instrument], None] = lambda inst: None   # feed subscription hook
        self.auto_trading = True            # False = strategies keep running but no new entries are sent
        self.option_market = option_market
        self.options_cfg = options_config or OptionsConfig()
        self._options_overrides = options_overrides or {}
        self.planner = OptionsPlanner(option_market, self.options_cfg) if option_market else None
        self._options_by_underlying: dict[str, set[str]] = {}   # underlying name -> option keys being tracked
        self._watchdog_fired: set[str] = set()
        self._squared_off: set[tuple[date, str]] = set()
        self._session_day: date | None = None
        self._eod_done: set[date] = set()
        self.oms = OrderManager(broker, portfolio, registry.get, events, clock, persist=self.repo.save_order,
                                on_trade=self._on_trade_closed, on_fill=self.repo.save_fill,
                                on_open=self._on_position_opened,
                                option_stop_buffer_pct=self.cfg.option_stop_buffer_pct)
        for s in strategies:
            self.add_strategy(s)

    # ================================================================ setup
    def add_strategy(self, s: Strategy) -> None:
        if s.id in self.strategies:
            raise ValueError(f"Duplicate strategy id {s.id}")
        keys = []
        for spec in s.instruments:
            inst = self.registry.resolve(spec)
            keys.append(inst.key)
            tfs = set(s.timeframes) | set(self.scorer.higher_timeframes(s.id))
            agg = self.aggregators.get(inst.key)
            if agg is None:
                agg = self.aggregators[inst.key] = MultiTimeframeAggregator(inst.token, tfs, self.cfg.base_timeframe)
            for tf in tfs:
                agg.add_timeframe(tf)
            ind = self.indicators.setdefault(inst.key, IndicatorEngine())
            extra = self.planner.indicators(s.timeframe) if (self.is_options(s) and self.planner) else []
            for spec_ in [*s.indicators(), *self.scorer.indicators(s.timeframe, s.id), *extra]:
                spec_ = spec_ if spec_.timeframe else spec_.with_timeframe(s.timeframe)
                ind.register(spec_, self.store.series(inst.key, spec_.timeframe).last())
            self._subs.setdefault((inst.key, s.timeframe), []).append(s.id)
        self.strategies[s.id] = s
        self.strategy_instruments[s.id] = keys
        self.stats.setdefault(s.id, {"signals": 0, "ignored_low_score": 0, "risk_rejected": 0, "approved": 0,
                                     "errors": 0, "consecutive_errors": 0, "status": "enabled" if s.enabled else "disabled"})

    def set_strategy_enabled(self, sid: str, enabled: bool) -> None:
        s = self.strategies[sid]
        s.enabled = enabled
        self.stats[sid]["status"] = "enabled" if enabled else "disabled"
        self.stats[sid]["consecutive_errors"] = 0
        self.events.emit("strategy_enabled" if enabled else "strategy_disabled", strategy=sid)

    @property
    def instruments(self) -> list[Instrument]:
        return [self.registry.get(k) for k in self.aggregators]

    @property
    def underlyings(self) -> list[Instrument]:
        """Instruments strategies analyse (excludes option contracts tracked for open trades)."""
        keys = {k for ks in self.strategy_instruments.values() for k in ks}
        return [self.registry.get(k) for k in self.aggregators if k in keys]

    @staticmethod
    def is_options(s: Strategy) -> bool:
        return getattr(s, "execution", "underlying") == "options"

    def options_config_for(self, sid: str) -> OptionsConfig:
        o = self._options_overrides.get(sid)
        if not o:
            return self.options_cfg
        from dataclasses import asdict
        return OptionsConfig.from_dict({**asdict(self.options_cfg), **o})

    def ensure_instrument(self, inst: Instrument, quote=None, timeframes: Iterable[str] = ()) -> None:
        """Start tracking an instrument at runtime (e.g. the option contract a plan selected): candles,
        last price, feed subscription. A chain quote seeds price/freshness until the first tick arrives."""
        if inst.key not in self.registry:
            self.registry.add(inst)
        if inst.key in self.aggregators:
            for tf in timeframes:
                self.aggregators[inst.key].add_timeframe(tf)
        else:
            self.aggregators[inst.key] = MultiTimeframeAggregator(inst.token, [self.cfg.base_timeframe, *timeframes],
                                                                  self.cfg.base_timeframe)
            self.indicators.setdefault(inst.key, IndicatorEngine())
            if inst.is_option:
                self._options_by_underlying.setdefault(inst.name, set()).add(inst.key)
            self.on_instrument_added(inst)
        if quote is not None:
            ts = self.clock.now()
            self.last_price[inst.key] = quote.ltp or quote.mid
            self.last_data_at[inst.key] = ts
            self._last_data_mono[inst.key] = time.monotonic()
            if isinstance(self.broker, PaperBroker):
                self.broker.on_quote(inst.key, quote.ltp or quote.mid, quote.bid, quote.ask, quote.bid_qty,
                                     quote.ask_qty, ts)

    def underlying_price(self, name: str) -> float | None:
        """Latest price of the analysed (non-option) instrument for an underlying name, e.g. NIFTY."""
        for k in self.aggregators:
            inst = self.registry.get(k)
            if inst.name == name and not inst.is_option and k in self.last_price:
                return self.last_price[k]
        return None

    def _tracked_options(self, underlying_name: str) -> list[Instrument]:
        """Option contracts on this underlying that have a position or working order."""
        keys = self._options_by_underlying.get(underlying_name, set())
        active = set(self.portfolio.positions) | {o.instrument for o in self.oms.working()}
        return [self.registry.get(k) for k in keys if k in active]

    def warmup(self, instrument: str, base_candles: Iterable[Candle]) -> int:
        """Feed historical base-timeframe candles to build candles/indicators without running strategies."""
        agg = self.aggregators[instrument]
        now = self.clock.now()
        n = 0
        for c in base_candles:
            if tfm.bucket_end(c.ts, agg.base_tf) > now:
                break  # still forming: never treat an incomplete candle as closed
            for closed in agg.on_base_candle(c):
                self.store.add(instrument, closed)
                self.indicators[instrument].on_candle(closed)
            self.last_price[instrument] = c.close
            n += 1
        return n

    # ================================================================ market data inputs
    def on_tick(self, tick: Tick) -> None:
        inst = self.registry.by_token(tick.instrument_token)
        if inst is None or inst.key not in self.aggregators:
            return
        key = inst.key
        ts = to_ist(tick.ts)
        self._maybe_new_session(ts.date())
        self.last_price[key] = tick.ltp
        self.last_data_at[key] = self.clock.now()
        self._last_data_mono[key] = time.monotonic()
        if isinstance(self.broker, PaperBroker):
            self.broker.on_quote(key, tick.ltp, tick.bid, tick.ask, tick.bid_qty, tick.ask_qty, ts)
        self.portfolio.mark(key, tick.ltp)
        if not inst.is_option:
            self._drive_model_options(inst, tick.ltp, ts)
            self._check_underlying_exits(key, tick.ltp, tick.ltp)
        for c in self.aggregators[key].on_tick(tick):
            self._on_candle(key, c)

    def _drive_model_options(self, underlying: Instrument, price: float, ts: datetime) -> None:
        """Synthetic sessions: reprice held/working model option contracts from the underlying."""
        m = self.option_market
        if not (m and m.is_model and underlying.name):
            return
        for oi in self._tracked_options(underlying.name):
            self.on_tick(m.model_tick(oi, price, ts))

    def on_bar(self, instrument: str, bar: Candle) -> None:
        """Backtest driver: fill working orders on this bar (at bar-open time), then close candles and
        run strategies at bar-close time. Orders created now can only fill on the next bar."""
        self._maybe_new_session(bar.ts.date())
        sim = isinstance(self.clock, SimClock)
        if sim:
            self.clock.set(bar.ts)
        end = tfm.bucket_end(bar.ts, self.cfg.base_timeframe)
        if isinstance(self.broker, PaperBroker):
            self.broker.on_bar(instrument, bar)
            inst = self.registry.get(instrument)
            m = self.option_market
            if m and m.is_model and inst.name and not inst.is_option:
                for oi in self._tracked_options(inst.name):
                    obar = m.model_bar(oi, bar, bar.ts, end)
                    self.broker.on_bar(oi.key, obar)
                    self.last_price[oi.key] = obar.close
                    self.portfolio.mark(oi.key, obar.close)
                    for c in self.aggregators[oi.key].on_base_candle(obar):
                        self.store.add(oi.key, c)
        if sim:
            self.clock.set(end)
        self.last_price[instrument] = bar.close
        self.last_data_at[instrument] = self.clock.now()
        self.portfolio.mark(instrument, bar.close)
        self._check_underlying_exits(instrument, bar.high, bar.low)
        for c in self.aggregators[instrument].on_base_candle(bar):
            self._on_candle(instrument, c)

    def on_timer(self, now: datetime | None = None) -> None:
        now = now or self.clock.now()
        for key, agg in self.aggregators.items():
            for c in agg.flush(now, self.cfg.candle_grace_seconds):
                self._on_candle(key, c)
        self._risk_housekeeping(now)
        self._stop_watchdog()
        self._square_off(now)
        self._end_of_day(now)

    # ================================================================ pipeline
    def _on_candle(self, key: str, c: Candle) -> None:
        self.store.add(key, c)
        self.indicators[key].on_candle(c)
        for sid in self._subs.get((key, c.timeframe), ()):
            s = self.strategies[sid]
            if s.enabled:
                self._run_strategy(s, key)

    def option_links(self, underlying_key: str, strategy: str | None = None) -> list:
        """Active option brackets whose trade idea is on `underlying_key`."""
        return [b for b in self.oms.brackets.values()
                if b.active and b.meta.get("underlying_key") == underlying_key
                and (strategy is None or b.strategy == strategy)]

    def _context(self, s: Strategy, key: str) -> StrategyContext:
        pos = self.portfolio.positions.get(key)
        view = None
        if self.is_options(s):
            # The strategy thinks in terms of the underlying: present its option trade as a long (bullish)
            # or short (bearish) underlying position with the underlying entry/stop/target.
            for b in self.option_links(key, s.id):
                opos = self.portfolio.positions.get(b.instrument)
                if opos is None:
                    continue
                sign = 1 if b.meta.get("direction") == "bullish" else -1
                view = PositionView(sign * abs(opos.quantity), b.meta.get("underlying_entry") or 0.0,
                                    b.meta.get("underlying_sl"), b.meta.get("underlying_target"), opos.opened_at)
                break
        elif pos is not None and pos.strategy == s.id:
            view = PositionView(pos.quantity, pos.avg_price, pos.stop_loss, pos.target, pos.opened_at)
        return StrategyContext(s.id, self.registry.get(key), s.timeframe, self.clock.now(), self.store,
                               self.indicators[key], view, s.params)

    def _run_strategy(self, s: Strategy, key: str) -> None:
        if len(self.store.series(key, s.timeframe)) < s.warmup_bars:
            return
        ctx = self._context(s, key)
        st = self.stats[s.id]
        try:
            out = s.on_candle(ctx)
            st["consecutive_errors"] = 0
        except Exception as e:  # a buggy strategy must not take the engine down
            st["errors"] += 1
            st["consecutive_errors"] += 1
            self.events.emit("strategy_error", "ERROR", strategy=s.id, instrument=key, error=repr(e))
            if st["consecutive_errors"] >= self.cfg.max_strategy_errors:
                s.enabled = False
                st["status"] = "disabled (errors)"
                self.events.emit("strategy_auto_disabled", "CRITICAL", strategy=s.id,
                                 reason=f"{st['consecutive_errors']} consecutive errors")
            return
        sigs = out if isinstance(out, list) else ([out] if out else [])
        for sig in sigs:
            self.process_signal(sig, ctx)

    def process_signal(self, sig: Signal, ctx: StrategyContext) -> RiskDecision | None:
        st = self.stats.setdefault(sig.strategy, {"signals": 0, "ignored_low_score": 0, "risk_rejected": 0,
                                                  "approved": 0, "errors": 0, "consecutive_errors": 0,
                                                  "status": "manual"})
        st["signals"] += 1
        sig.ts = sig.ts or self.clock.now()
        self.events.emit("signal_generated", strategy=sig.strategy, instrument=sig.instrument, action=str(sig.action),
                         reason="; ".join(sig.reason), entry=sig.entry_price, stop_loss=sig.stop_loss, target=sig.target)
        try:
            sc = self.scorer.score(sig, ctx)
        except Exception as e:
            self.events.emit("scoring_error", "ERROR", strategy=sig.strategy, instrument=sig.instrument, error=repr(e))
            return None
        sig.score, sig.score_breakdown = sc.score, sc.breakdown
        sig.confidence = round(sc.score / 100, 3)
        rec = {**sig.as_dict(), "grade": sc.grade, "status": "", "risk": None}
        if not sc.passed:
            st["ignored_low_score"] += 1
            rec["status"] = "ignored_low_score"
            self._record_signal(rec)
            self.events.emit("signal_ignored", strategy=sig.strategy, instrument=sig.instrument,
                             reason=f"score {sc.score} < {self.scorer.cfg_for(sig.strategy).min_score}")
            return None

        strategy = self.strategies.get(sig.strategy)
        if strategy is not None and self.is_options(strategy):
            return self._process_option_signal(sig, ctx, sc, rec, st)

        if not self.auto_trading and sig.action != SignalAction.EXIT:
            rec["status"] = "paused"
            self._record_signal(rec)
            return None
        inst = self.registry.get(sig.instrument)
        decision = self._evaluate_risk(sig, inst)
        rec["risk"] = decision.as_dict()
        if not decision.approved:
            st["risk_rejected"] += 1
            rec["status"] = "risk_rejected"
            self._record_signal(rec)
            self.events.emit("risk_check_rejected", "WARNING", strategy=sig.strategy, instrument=sig.instrument,
                             action=str(sig.action), reason="; ".join(decision.reasons))
            return decision
        st["approved"] += 1
        rec["status"] = "approved"
        rec["quantity"] = decision.quantity
        self._record_signal(rec)
        self.events.emit("risk_check_passed", strategy=sig.strategy, instrument=sig.instrument,
                         action=str(sig.action), quantity=decision.quantity)
        try:
            if sig.action == SignalAction.EXIT:
                self.oms.exit_position(sig.instrument, "; ".join(sig.reason) or "strategy exit")
            else:
                product = Product(ctx.params.get("product", self.cfg.default_product))
                self.oms.submit_entry(sig, decision.quantity, product)
        except BrokerError as e:
            self.events.emit("broker_api_error", "ERROR", strategy=sig.strategy, instrument=sig.instrument, error=str(e))
        return decision

    def _process_option_signal(self, sig: Signal, ctx: StrategyContext, sc, rec: dict, st: dict) -> RiskDecision | None:
        """Underlying signal -> option trade (or exit of the linked option trade)."""
        underlying = ctx.instrument
        if sig.action == SignalAction.EXIT:
            links = self.option_links(underlying.key, sig.strategy)
            rec["status"] = "approved" if links else "no_position"
            self._record_signal(rec)
            for b in links:
                b.meta["exit_trigger"] = "strategy exit"
                self.oms.exit_position(b.instrument, "strategy exit: " + ("; ".join(sig.reason) or "signal"))
            return None
        if any(b.meta.get("underlying_key") == underlying.key for b in self.oms.brackets.values() if b.active):
            rec["status"] = "already_in_position"
            self._record_signal(rec)
            return None
        if not self.auto_trading:
            rec["status"] = "paused"
            self._record_signal(rec)
            return None
        if self.planner is None:
            rec["status"] = "no_contract"
            rec["risk"] = {"approved": False, "reasons": ["no option market configured"], "checks": []}
            self._record_signal(rec)
            return None
        cfg = self.options_config_for(sig.strategy)
        plan = self.planner.plan(sig, ctx, underlying, sc.grade, cfg)
        if not plan.ok:
            st["risk_rejected"] += 1
            rec["status"] = "no_contract"
            rec["risk"] = {"approved": False, "reasons": [plan.reason], "checks": []}
            rec["metadata"] = {**(rec.get("metadata") or {}), "decision_notes": plan.notes,
                               "option_action": plan.action}
            self._record_signal(rec)
            self.events.emit("option_plan_failed", "WARNING", strategy=sig.strategy, instrument=underlying.key,
                             reason=plan.reason, action=plan.action)
            return None
        opt = plan.signal
        inst = plan.quote.instrument
        self.ensure_instrument(inst, plan.quote)
        self.events.emit("option_trade_planned", strategy=sig.strategy, instrument=inst.key, action=plan.action,
                         reason="; ".join(plan.notes), entry=opt.entry_price, stop_loss=opt.stop_loss,
                         target=opt.target)
        decision = self._evaluate_risk(opt, inst)
        orec = {**opt.as_dict(), "grade": sc.grade, "risk": decision.as_dict(), "status": ""}
        if not decision.approved:
            st["risk_rejected"] += 1
            orec["status"] = "risk_rejected"
            self._record_signal(orec)
            self.events.emit("risk_check_rejected", "WARNING", strategy=sig.strategy, instrument=inst.key,
                             action=plan.action, reason="; ".join(decision.reasons))
            return decision
        st["approved"] += 1
        orec["status"] = "approved"
        orec["quantity"] = decision.quantity
        self._record_signal(orec)
        self.events.emit("risk_check_passed", strategy=sig.strategy, instrument=inst.key, action=plan.action,
                         quantity=decision.quantity)
        try:
            self.oms.submit_entry(opt, decision.quantity, Product(cfg.product))
        except BrokerError as e:
            self.events.emit("broker_api_error", "ERROR", strategy=sig.strategy, instrument=inst.key, error=str(e))
        return decision

    def _check_underlying_exits(self, underlying_key: str, high: float, low: float) -> None:
        """Exit option trades when the underlying reaches the strategy's stop or target."""
        for b in self.option_links(underlying_key):
            if b.instrument not in self.portfolio.positions or self.oms.working(b.instrument, OrderRole.EXIT):
                continue
            m = b.meta
            bull = m.get("direction") == "bullish"
            sl, tgt = m.get("underlying_sl"), m.get("underlying_target")
            reason = None
            if sl is not None and ((bull and low <= sl) or (not bull and high >= sl)):
                reason = f"underlying stop {sl} hit"
            elif tgt is not None and ((bull and high >= tgt) or (not bull and low <= tgt)):
                reason = f"underlying target {tgt} hit"
            if reason:
                m["exit_trigger"] = reason
                self.oms.exit_position(b.instrument, reason)

    def _stop_watchdog(self) -> None:
        """A triggered stop-limit can be jumped by a fast market. If price is beyond the stop's limit
        and the position is still open, exit at market."""
        for b in list(self.oms.brackets.values()):
            if not b.active or not b.sl_id or b.instrument not in self.portfolio.positions:
                continue
            o = self.oms.orders.get(b.sl_id)
            if o is None or o.order_type != OrderType.SL or not o.status.is_working or o.price is None:
                continue
            last = self.last_price.get(b.instrument)
            if last is None or b.sl_id in self._watchdog_fired:
                continue
            beyond = last < o.price if o.side.value == "SELL" else last > o.price
            if beyond:
                self._watchdog_fired.add(b.sl_id)
                self.events.emit("stop_watchdog", "WARNING", instrument=b.instrument, order_id=o.client_order_id,
                                 reason=f"LTP {last} beyond stop limit {o.price}; exiting at market")
                self.oms.exit_position(b.instrument, "stop-limit not filled; market exit")

    # ---------------------------------------------------------------- trade journal
    def _on_position_opened(self, pos, bracket) -> None:
        m = dict(bracket.meta) if bracket else {}
        inst = self.registry.get(pos.instrument)
        risk_unit = abs(pos.avg_price - bracket.stop_loss) if (bracket and bracket.stop_loss) else None
        self.repo.journal_open({
            "position_id": pos.position_id, "strategy": pos.strategy, "instrument": pos.instrument,
            "underlying": m.get("underlying") or inst.name or None,
            "option_type": inst.instrument_type if inst.is_option else None,
            "action": m.get("option_action") or ("BUY" if pos.quantity > 0 else "SELL"),
            "direction": m.get("direction") or ("bullish" if pos.quantity > 0 else "bearish"),
            "strike": inst.strike, "expiry": str(inst.expiry) if inst.expiry else None, "dte": m.get("dte"),
            "lot_size": inst.lot_size, "quantity": abs(pos.quantity),
            "lots": abs(pos.quantity) // max(inst.lot_size, 1),
            "entry_time": pos.opened_at.isoformat(), "entry_price": pos.avg_price,
            "stop_loss": bracket.stop_loss if bracket else None, "target": bracket.target if bracket else None,
            "underlying_entry": m.get("underlying_entry"), "underlying_sl": m.get("underlying_sl"),
            "underlying_target": m.get("underlying_target"), "planned_rr": m.get("planned_rr"),
            "planned_risk": round(risk_unit * abs(pos.quantity), 2) if risk_unit else None,
            "iv": m.get("iv"), "delta": m.get("delta"), "atm_iv": m.get("atm_iv"),
            "realized_vol": m.get("realized_vol"), "adx": m.get("adx"), "score": m.get("score"),
            "confidence": m.get("confidence"), "timeframe": m.get("timeframe"), "reasons": m.get("reasons"),
            "decision_notes": m.get("decision_notes"), "entry_charges": round(pos.entry_charges, 2),
        })

    def _on_trade_closed(self, t, bracket=None) -> None:
        self.repo.save_trade(t)
        u_key = bracket.meta.get("underlying_key") if bracket else None
        self.repo.journal_close(t.position_id, {
            "exit_time": t.exit_ts.isoformat(), "exit_price": t.exit_price, "exit_reason": t.exit_reason,
            "exit_quantity": t.quantity, "gross_pnl": t.gross_pnl, "charges": t.charges,
            "net_pnl": round(t.net_pnl, 2), "mfe": t.mfe, "mae": t.mae,
            "underlying_exit": self.last_price.get(u_key) if u_key else None,
            "duration_s": int((t.exit_ts - t.entry_ts).total_seconds()),
            "closed": t.instrument not in self.portfolio.positions,
        })

    def _evaluate_risk(self, sig: Signal, inst: Instrument) -> RiskDecision:
        now = self.clock.now()
        last = self.last_price.get(sig.instrument) or sig.entry_price or 0.0
        age = self.data_age(sig.instrument)
        price = sig.entry_price or last

        def margin_for(qty: int) -> float:
            o = Order(sig.instrument, sig.side, qty, OrderType.MARKET, self.cfg.default_product, OrderRole.ENTRY, sig.strategy)
            return self.broker.estimate_margin(o, inst, price)

        try:
            account = self.broker.get_account_balance()
        except BrokerError as e:
            from app.risk.manager import RiskCheck
            return RiskDecision(False, 0, [RiskCheck("broker_account", False, f"cannot read account: {e}")])
        inp = RiskInputs(now, inst, self.portfolio, account, last, age, self.oms.pending_entry_instruments(),
                         margin_for if sig.action != SignalAction.EXIT else None, self.cfg.enforce_session_times)
        decision = self.risk.evaluate(sig, inp)
        if sig.action != SignalAction.EXIT:
            gate = self.entry_gate()
            if gate:
                from app.risk.manager import RiskCheck
                decision.checks.append(RiskCheck("execution_gate", False, gate))
                decision.approved, decision.quantity = False, 0
        return decision

    def _record_signal(self, rec: dict) -> None:
        self.signals.appendleft(rec)
        self.repo.save_signal(rec)

    # ================================================================ session management
    def _maybe_new_session(self, day: date) -> None:
        if self._session_day == day:
            return
        first = self._session_day is None
        self._session_day = day
        if not first:
            self.events.emit("new_session", reason=str(day))
            # Day orders (incl. protective stops) expired overnight: re-arm protection for carried positions.
            for b in list(self.oms.brackets.values()):
                if b.active:
                    self.oms._ensure_protection(b)

    def _risk_housekeeping(self, now: datetime) -> None:
        reason = self.risk.check_breaches(now, self.portfolio)
        if reason:
            self.events.emit("risk_limit_breached", "CRITICAL", reason=reason)
            if self.risk.kill_switch.active:
                self.kill_switch(self.risk.kill_switch.reason, flatten=True)
            elif self.risk.cfg.flatten_on_daily_halt:
                self.oms.flatten_all(f"daily halt: {reason}")
            self.repo.save_state("risk", self.risk_state())

    def _square_off(self, now: datetime) -> None:
        if now.time() >= parse_hhmm(self.cfg.expiry_day_exit_time):
            for key in list(self.portfolio.positions):
                inst = self.registry.get(key)
                if inst.is_option and inst.expiry == now.date() and (now.date(), key) not in self._squared_off:
                    self._squared_off.add((now.date(), key))
                    self.oms.exit_position(key, "expiry-day exit")
        if now.time() < parse_hhmm(self.risk.cfg.square_off_time):
            return
        for key, pos in list(self.portfolio.positions.items()):
            b = self.oms.bracket_for(key)
            product = b.product if b else self.cfg.default_product
            if product != Product.MIS or (now.date(), key) in self._squared_off:
                continue
            self._squared_off.add((now.date(), key))
            self.oms.exit_position(key, "intraday square-off")

    def _end_of_day(self, now: datetime) -> None:
        if now.time() < SESSION_CLOSE or now.date() in self._eod_done:
            return
        self._eod_done.add(now.date())
        if isinstance(self.broker, PaperBroker):
            self.broker.expire_day_orders(datetime.combine(now.date(), SESSION_CLOSE, now.tzinfo))
        self.portfolio.snapshot(now)
        self.events.emit("end_of_day", reason=str(now.date()), equity=round(self.portfolio.equity(), 2))

    # ================================================================ controls
    def kill_switch(self, reason: str, flatten: bool = True) -> None:
        now = self.clock.now()
        if not self.risk.kill_switch.active:
            self.risk.trip_kill_switch(reason, now)
        self.events.emit("kill_switch_activated", "CRITICAL", reason=reason, flatten=flatten)
        self.oms.cancel_all(f"kill switch: {reason}")
        if flatten:
            self.oms.flatten_all(f"kill switch: {reason}")
        self.repo.save_state("risk", self.risk_state())

    def reset_kill_switch(self) -> None:
        self.risk.reset_kill_switch()
        self.events.emit("kill_switch_reset", "WARNING")
        self.repo.save_state("risk", self.risk_state())

    def risk_state(self) -> dict:
        ks = self.risk.kill_switch
        return {"kill_switch": {"active": ks.active, "reason": ks.reason, "ts": ks.ts.isoformat() if ks.ts else None},
                "daily_halts": {str(d): r for d, r in self.risk.daily_halts.items()}}

    def load_risk_state(self, st: dict | None) -> None:
        if not st:
            return
        ks = st.get("kill_switch") or {}
        if ks.get("active"):
            self.risk.trip_kill_switch(ks.get("reason", "restored"), datetime.fromisoformat(ks["ts"]) if ks.get("ts")
                                       else self.clock.now())
        for d, r in (st.get("daily_halts") or {}).items():
            self.risk.daily_halts[date.fromisoformat(d)] = r

    # ================================================================ views
    def data_age(self, instrument: str) -> float:
        """Seconds since the last market data for `instrument`, in *wall-clock* time (feed liveness is a
        real-world property; the engine clock may be simulated). Backtests are never stale."""
        if self.mode == TradingMode.BACKTEST:
            return 0.0
        seen = self._last_data_mono.get(instrument)
        return time.monotonic() - seen if seen is not None else 1e9

    def market_status(self) -> dict[str, dict]:
        """Freshness of analysed instruments and of option contracts currently held/working."""
        out = {}
        active = set(self.portfolio.positions) | {o.instrument for o in self.oms.working()}
        keys = [i.key for i in self.underlyings] + [k for k in self.aggregators if k in active]
        for key in dict.fromkeys(keys):
            age = self.data_age(key)
            known = age < 1e9
            out[key] = {"last_price": self.last_price.get(key), "age_s": round(age, 1) if known else None,
                        "stale": age > self.risk.cfg.stale_data_seconds}
        return out

    def overview(self) -> dict:
        now = self.clock.now()
        acct = self.broker.get_account_balance()
        pf = self.portfolio
        day = now.date()
        today = pf.trades_on(day)
        all_trades = pf.trades
        dd, dd_pct = pf.drawdown_now()
        return {
            "mode": str(self.mode), "now": now.isoformat(),
            "capital": pf.capital, "equity": round(pf.equity(), 2),
            "cash": acct.cash, "available_margin": acct.available_margin, "used_margin": acct.used_margin,
            "todays_pnl": round(pf.day_pnl(day), 2),
            "unrealized_pnl": round(pf.unrealized, 2),
            "realized_pnl": round(pf.realized_net, 2),
            "charges": round(pf.total_charges, 2),
            "trades_today": len(today), "entries_today": pf.entries_on(day),
            "total_trades": len(all_trades),
            "win_rate_pct": round(100 * sum(t.net_pnl > 0 for t in all_trades) / len(all_trades), 2) if all_trades else 0.0,
            "drawdown": round(dd, 2), "drawdown_pct": round(dd_pct, 2),
            "max_drawdown": round(pf.max_drawdown, 2), "max_drawdown_pct": round(pf.max_drawdown_pct, 2),
            "open_positions": len(pf.positions),
            "kill_switch": self.risk_state()["kill_switch"],
            "daily_halt": self.risk.halted(day),
        }

    def positions_view(self) -> list[dict]:
        now = self.clock.now()
        out = []
        for p in self.portfolio.positions.values():
            d = p.as_dict(now)
            b = self.oms.bracket_for(p.instrument)
            if b:
                m = b.meta
                for k in ("option_action", "underlying", "underlying_key", "underlying_entry", "underlying_sl",
                          "underlying_target", "planned_rr", "dte", "delta", "iv", "score"):
                    d[k] = m.get(k)
                if m.get("underlying_key"):
                    d["underlying_last"] = self.last_price.get(m["underlying_key"])
            inst = self.registry.get(p.instrument)
            d["lots"] = abs(p.quantity) // max(inst.lot_size, 1)
            d["lot_size"] = inst.lot_size
            out.append(d)
        return out

    def strategies_view(self) -> list[dict]:
        per = metrics.by_strategy(self.portfolio.trades)
        out = []
        for sid, s in self.strategies.items():
            d = s.describe()
            d["instruments_resolved"] = self.strategy_instruments.get(sid, [])
            d.update(self.stats.get(sid, {}))
            d.update(per.get(sid, {"trades": 0, "win_rate_pct": 0.0, "net_pnl": 0.0, "max_drawdown": 0.0}))
            d["open_positions"] = sum(1 for p in self.portfolio.positions.values() if p.strategy == sid)
            out.append(d)
        return out

    def orders_view(self) -> list[dict]:
        return sorted((o.as_dict() for o in self.oms.orders.values()), key=lambda d: d["created_at"] or 0, reverse=True)
