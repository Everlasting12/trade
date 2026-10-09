"""Strategy interface.

A strategy is pure decision logic. It receives a read-only `StrategyContext` (closed candles,
indicators, patterns, its own position) on every closed candle of its timeframe and returns zero
or more `Signal`s. It never talks to a broker, never sees unfinished candles and never knows
whether it runs in a backtest, paper or live: that is what makes results comparable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.core.models import Candle, Instrument, Signal, SignalAction
from app.indicators.base import IndicatorSpec
from app.indicators.engine import IndicatorEngine
from app.market_data.candles import CandleStore
from app.patterns import candlestick, chart_patterns, price_action
from app.patterns.base import PatternResult


@dataclass
class PositionView:
    quantity: int            # signed
    avg_price: float
    stop_loss: float | None
    target: float | None
    opened_at: datetime | None


@dataclass
class StrategyContext:
    strategy_id: str
    instrument: Instrument
    timeframe: str
    now: datetime
    store: CandleStore
    indicators: IndicatorEngine
    position: PositionView | None = None
    params: dict[str, Any] = field(default_factory=dict)
    option_chain: Callable[[], Any] | None = None   # lazily fetched by the engine when a strategy needs it
    _cache: dict = field(default_factory=dict)

    # ---- candles ----
    def candles(self, n: int | None = None, tf: str | None = None) -> list[Candle]:
        return self.store.series(self.instrument.key, tf or self.timeframe).last(n)

    @property
    def candle(self) -> Candle:
        return self.store.series(self.instrument.key, self.timeframe)[-1]

    @property
    def position_qty(self) -> int:
        return self.position.quantity if self.position else 0

    # ---- indicators ----
    def ind(self, name: str, tf: str | None = None, offset: int = 0, **params) -> Any:
        return self.indicators.value(IndicatorSpec.of(name, tf or self.timeframe, **params), offset)

    def crossed_above(self, a: tuple, b: tuple) -> bool:
        """a, b = (name, params_dict) - true if a crossed above b on the latest candle."""
        a0, a1 = self.ind(a[0], **a[1]), self.ind(a[0], offset=1, **a[1])
        b0, b1 = self.ind(b[0], **b[1]), self.ind(b[0], offset=1, **b[1])
        return None not in (a0, a1, b0, b1) and a1 <= b1 and a0 > b0

    def crossed_below(self, a: tuple, b: tuple) -> bool:
        a0, a1 = self.ind(a[0], **a[1]), self.ind(a[0], offset=1, **a[1])
        b0, b1 = self.ind(b[0], **b[1]), self.ind(b[0], offset=1, **b[1])
        return None not in (a0, a1, b0, b1) and a1 >= b1 and a0 < b0

    # ---- patterns (computed lazily, cached per candle) ----
    def price_action(self, tf: str | None = None) -> price_action.PriceActionSnapshot:
        key = ("pa", tf or self.timeframe)
        if key not in self._cache:
            self._cache[key] = price_action.analyze(self.candles(200, tf), tf or self.timeframe)
        return self._cache[key]

    def candle_patterns(self, tf: str | None = None, lookback: int = 3) -> list[PatternResult]:
        key = ("cs", tf or self.timeframe, lookback)
        if key not in self._cache:
            self._cache[key] = candlestick.detect_recent(self.candles(40, tf), lookback, tf or self.timeframe)
        return self._cache[key]

    def chart_patterns(self, tf: str | None = None) -> list[PatternResult]:
        key = ("cp", tf or self.timeframe)
        if key not in self._cache:
            self._cache[key] = chart_patterns.detect(self.candles(150, tf), tf or self.timeframe)
        return self._cache[key]

    # ---- signal helpers ----
    def signal(self, action: SignalAction | str, **kw) -> Signal:
        c = self.candle
        kw.setdefault("entry_price", c.close)
        return Signal(action=SignalAction(action), instrument=self.instrument.key, strategy=self.strategy_id,
                      timeframe=self.timeframe, ts=self.now, **kw)

    def exit(self, *reason: str) -> Signal:
        return self.signal(SignalAction.EXIT, reason=list(reason))


class Strategy(ABC):
    name: str = "base"
    description: str = ""
    default_params: dict[str, Any] = {}

    def __init__(self, instruments: list[str], timeframe: str = "5m", params: dict | None = None,
                 strategy_id: str | None = None, enabled: bool = True, higher_timeframes: list[str] | None = None,
                 execution: str = "underlying"):
        self.id = strategy_id or self.name
        self.instruments = list(instruments)
        self.timeframe = timeframe
        self.params = {**self.default_params, **(params or {})}
        self.enabled = enabled
        self.higher_timeframes = list(higher_timeframes or [])
        if execution not in ("underlying", "options"):
            raise ValueError("execution must be 'underlying' or 'options'")
        self.execution = execution   # options = trade CE/PE chosen by the options planner

    def spec(self, name: str, tf: str | None = None, **params) -> IndicatorSpec:
        return IndicatorSpec.of(name, tf or self.timeframe, **params)

    def indicators(self) -> list[IndicatorSpec]:
        """Indicators this strategy reads via ctx.ind(); registered and warmed up by the engine."""
        return []

    @property
    def warmup_bars(self) -> int:
        return int(self.params.get("warmup_bars", 60))

    @property
    def timeframes(self) -> list[str]:
        return [self.timeframe, *self.higher_timeframes]

    @abstractmethod
    def on_candle(self, ctx: StrategyContext) -> Signal | list[Signal] | None: ...

    # ---- shared helpers ----
    @staticmethod
    def atr_bracket(entry: float, atr: float, long: bool, atr_mult: float, rr: float) -> tuple[float, float]:
        risk = atr * atr_mult
        if long:
            return entry - risk, entry + rr * risk
        return entry + risk, entry - rr * risk

    def describe(self) -> dict:
        return {"id": self.id, "type": self.name, "description": self.description, "timeframe": self.timeframe,
                "higher_timeframes": self.higher_timeframes, "instruments": self.instruments,
                "execution": self.execution,
                "params": self.params, "enabled": self.enabled}
