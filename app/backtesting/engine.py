"""Event-driven backtester that reuses the live TradingEngine unchanged.

Look-ahead protection
- Strategies run only on *closed* candles; the engine clock is the candle close time.
- Orders created on a candle close are filled by the PaperBroker on the *next* bar (at its open,
  with slippage), never on the bar that generated them.
- Higher-timeframe candles are emitted only when the base candle completing them has closed.
- Indicators are incremental and only ever see closed candles.

Known simplifications (documented, conservative where possible)
- Intrabar path is unknown: when stop and target are both inside one bar the stop is assumed first.
- Fills beyond top-of-book are approximated by a share-of-bar-volume cap (partial fills).
- Margin uses a percentage approximation unless a broker margin API is available.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from app.core.clock import IST, SESSION_CLOSE, SimClock
from app.core.models import Candle, Product, TradingMode
from app.engine.engine import EngineConfig, TradingEngine
from app.execution.charges import ChargesConfig, ChargesModel
from app.execution.paper.broker import PaperBroker, PaperConfig
from app.market_data import timeframes as tfm
from app.market_data.instruments import InstrumentRegistry
from app.options.market import ModelOptionMarket
from app.options.planner import OptionsConfig
from app.portfolio import metrics
from app.portfolio.portfolio import Portfolio
from app.risk.manager import RiskConfig, RiskManager
from app.signals.scoring import ScoringConfig, SignalScorer
from app.strategies.base import Strategy
from app.utils.events import NullEventLog


@dataclass
class BacktestConfig:
    capital: float = 500_000.0
    base_timeframe: str = "1m"
    product: Product = Product.MIS
    paper: PaperConfig = field(default_factory=PaperConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    scoring_per_strategy: dict[str, ScoringConfig] = field(default_factory=dict)
    charges: ChargesConfig = field(default_factory=ChargesConfig)
    enforce_session_times: bool = True
    max_equity_points: int = 3000
    options: OptionsConfig = field(default_factory=OptionsConfig)
    options_overrides: dict = field(default_factory=dict)
    option_iv: float = 0.14            # model IV for option pricing (options backtests are approximate)
    option_expiry_weekday: int = 1     # weekly expiry weekday for the model chain (Mon=0)


class MemoryJournal:
    """In-memory trade journal for backtests (same rows as the paper/live `journal` table)."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def journal_open(self, row: dict) -> None:
        self.rows[row["position_id"]] = {**row, "status": "open"}

    def journal_close(self, position_id: str, ex: dict) -> None:
        r = self.rows.get(position_id)
        if r is None:
            return
        prev_q = r.get("exit_quantity") or 0
        q = prev_q + ex["exit_quantity"]
        r["exit_price"] = round(((r.get("exit_price") or 0) * prev_q + ex["exit_price"] * ex["exit_quantity"]) / q, 2)
        r["exit_quantity"] = q
        for k in ("gross_pnl", "charges", "net_pnl"):
            r[k] = round((r.get(k) or 0) + (ex.get(k) or 0), 2)
        r.update({k: ex.get(k) for k in ("exit_time", "exit_reason", "underlying_exit", "mfe", "mae", "duration_s")})
        r["r_multiple"] = round(r["net_pnl"] / r["planned_risk"], 2) if r.get("planned_risk") else None
        r["status"] = "closed" if ex.get("closed", True) else "open"

    def __getattr__(self, name):  # every other repository call is a no-op in backtests
        return lambda *a, **k: None


@dataclass
class BacktestResult:
    metrics: dict
    trades: list[dict]
    equity_curve: list[tuple[str, float]]
    signals: list[dict]
    orders: list[dict]
    strategies: list[dict]
    warnings: list[dict]
    bars_processed: int
    journal: list[dict] = field(default_factory=list)
    candles: list[dict] = field(default_factory=list)   # strategy-timeframe candles of the first instrument


def run_backtest(strategies: list[Strategy], data: dict[str, list[Candle]], registry: InstrumentRegistry,
                 config: BacktestConfig | None = None) -> BacktestResult:
    """`data` maps instrument key (as resolved by the registry) -> base-timeframe candles."""
    cfg = config or BacktestConfig()
    all_bars = [c for bars in data.values() for c in bars]
    if not all_bars:
        raise ValueError("No candles to backtest")
    clock = SimClock(min(c.ts for c in all_bars))
    cfg.paper.capital = cfg.capital
    portfolio = Portfolio(cfg.capital)
    broker = PaperBroker(registry.get, cfg.paper, ChargesModel(cfg.charges), clock)
    broker.mode = TradingMode.BACKTEST
    events = NullEventLog(clock)
    holder: dict = {}
    option_market = None
    if any(getattr(s, "execution", "underlying") == "options" for s in strategies):
        option_market = ModelOptionMarket(registry, lambda name: holder["engine"].underlying_price(name), clock,
                                          iv=cfg.option_iv, expiry_weekday=cfg.option_expiry_weekday)
    engine = TradingEngine(
        mode=TradingMode.BACKTEST, broker=broker, registry=registry, strategies=strategies,
        risk=RiskManager(cfg.risk), scorer=SignalScorer(cfg.scoring, cfg.scoring_per_strategy), portfolio=portfolio,
        events=events, clock=clock,
        config=EngineConfig(base_timeframe=cfg.base_timeframe, default_product=cfg.product,
                            enforce_session_times=cfg.enforce_session_times),
        option_market=option_market, options_config=cfg.options, options_overrides=cfg.options_overrides,
        repo=MemoryJournal())
    holder["engine"] = engine

    streams = {k: sorted(v, key=lambda c: c.ts) for k, v in data.items()}
    heap: list[tuple[datetime, str, int]] = [(bars[0].ts, k, 0) for k, bars in streams.items() if bars]
    heapq.heapify(heap)
    last_bar: dict[str, Candle] = {}
    day = None
    n = 0
    while heap:
        ts, key, i = heapq.heappop(heap)
        bar = streams[key][i]
        if day is not None and bar.ts.date() != day:
            _close_day(engine, broker, last_bar, day, final=False)
        day = bar.ts.date()
        engine.on_bar(key, bar)
        end = tfm.bucket_end(bar.ts, cfg.base_timeframe)
        engine.on_timer(end)
        portfolio.snapshot(end)
        last_bar[key] = bar
        n += 1
        if i + 1 < len(streams[key]):
            heapq.heappush(heap, (streams[key][i + 1].ts, key, i + 1))
    if day is not None:
        _close_day(engine, broker, last_bar, day, final=True)

    curve = portfolio.equity_curve
    step = max(1, len(curve) // cfg.max_equity_points)
    sampled = curve[::step] + ([curve[-1]] if curve and (len(curve) - 1) % step else [])
    return BacktestResult(
        metrics=metrics.compute(portfolio.trades, curve, cfg.capital),
        trades=[t.as_dict() for t in portfolio.trades],
        equity_curve=[(ts.isoformat(), round(eq, 2)) for ts, eq in sampled],
        signals=list(engine.signals),
        orders=engine.orders_view(),
        strategies=engine.strategies_view(),
        warnings=events.recent(500, level="WARNING") + events.recent(200, level="ERROR"),
        bars_processed=n,
        journal=sorted(engine.repo.rows.values(), key=lambda r: r["entry_time"]),
        candles=[c.as_dict() for c in engine.store.series(next(iter(data)), strategies[0].timeframe).last(3000)]
        if strategies else [],
    )


def _close_day(engine: TradingEngine, broker: PaperBroker, last_bar: dict[str, Candle], day, final: bool) -> None:
    """Make sure intraday positions are closed at the day's last price (and everything at the end of the
    test) using a synthetic zero-range bar at the close, then expire day orders."""
    for key, pos in list(engine.portfolio.positions.items()):
        b = engine.oms.bracket_for(key)
        product = b.product if b else engine.cfg.default_product
        if final or product == Product.MIS:
            engine.oms.exit_position(key, "end of backtest" if final else "end of day (forced)")
    for key, bar in last_bar.items():
        if bar.ts.date() != day:
            continue
        end = tfm.bucket_end(bar.ts, bar.timeframe)
        engine.clock.set(end)
        if engine.oms.working(key):
            close_bar = Candle(bar.ts, bar.close, bar.close, bar.close, bar.close, 0.0, None, bar.timeframe)
            broker.on_bar(key, close_bar)
        # Option contracts on this underlying: fill their exits at the model price at the close.
        m = engine.option_market
        inst = engine.registry.get(key)
        if m is not None and m.is_model and inst.name:
            for oi in engine._tracked_options(inst.name):
                px = m.premium(oi, bar.close, end)
                broker.on_bar(oi.key, Candle(bar.ts, px, px, px, px, 0.0, None, bar.timeframe))
    broker.expire_day_orders(None if final else datetime.combine(day, SESSION_CLOSE, IST))
    if engine.portfolio.equity_curve:
        engine.portfolio.snapshot(engine.portfolio.equity_curve[-1][0])


def single_instrument_registry(instruments: Iterable) -> InstrumentRegistry:
    return InstrumentRegistry(instruments)
