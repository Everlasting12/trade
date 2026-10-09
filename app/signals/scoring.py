"""Signal scoring.

Strategies *propose* entries; the scorer independently checks the proposal against a common set of
market-context factors and produces a 0-100 score. Weights, thresholds and factor parameters are
configuration (config/strategies.yaml -> `scoring`), not code.

Score = 100 * (sum of weights of passed factors) / (sum of weights of *applicable* factors).
A factor is "not applicable" when its data is missing (e.g. VWAP/volume on an index with no
volume) so that instruments are not penalised for data they cannot have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import Signal, SignalAction
from app.indicators.base import IndicatorSpec
from app.strategies.base import StrategyContext

DEFAULT_WEIGHTS = {
    "trend": 20, "ema": 15, "rsi": 10, "vwap": 15, "volume": 15, "price_action": 15, "candlestick": 10,
    "htf_trend": 0,
}


@dataclass
class ScoringConfig:
    enabled: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    strong_threshold: float = 75
    moderate_threshold: float = 60
    min_score: float = 60            # signals below this are ignored
    fast_ema: int = 20
    slow_ema: int = 50
    rsi_period: int = 14
    rsi_long_range: tuple[float, float] = (50, 75)
    rsi_short_range: tuple[float, float] = (25, 50)
    volume_ratio_min: float = 1.2
    trend_slope_bars: int = 5
    higher_timeframe: str = "15m"

    @classmethod
    def from_dict(cls, d: dict | None) -> "ScoringConfig":
        d = dict(d or {})
        w = {**DEFAULT_WEIGHTS, **d.pop("weights", {})}
        for k in ("rsi_long_range", "rsi_short_range"):
            if k in d:
                d[k] = tuple(d[k])
        return cls(weights=w, **d)


@dataclass
class ScoreResult:
    score: float
    grade: str                       # strong | moderate | weak
    passed: bool
    breakdown: dict[str, dict[str, Any]]


class SignalScorer:
    """Scores with a default config, optionally overridden per strategy id (e.g. mean-reversion
    strategies should not be scored with trend-following weights)."""

    def __init__(self, config: ScoringConfig | None = None, per_strategy: dict[str, ScoringConfig] | None = None):
        self.cfg = config or ScoringConfig()
        self.per_strategy = dict(per_strategy or {})

    def cfg_for(self, strategy_id: str | None) -> ScoringConfig:
        return self.per_strategy.get(strategy_id, self.cfg) if strategy_id else self.cfg

    def higher_timeframes(self, strategy_id: str | None = None) -> list[str]:
        c = self.cfg_for(strategy_id)
        return [c.higher_timeframe] if c.weights.get("htf_trend") and c.higher_timeframe else []

    def indicators(self, tf: str, strategy_id: str | None = None) -> list[IndicatorSpec]:
        c = self.cfg_for(strategy_id)
        specs = [IndicatorSpec.of("ema", tf, period=c.fast_ema), IndicatorSpec.of("ema", tf, period=c.slow_ema),
                 IndicatorSpec.of("rsi", tf, period=c.rsi_period), IndicatorSpec.of("vwap", tf),
                 IndicatorSpec.of("volume_ratio", tf, period=20)]
        if c.weights.get("htf_trend") and c.higher_timeframe:
            specs.append(IndicatorSpec.of("ema", c.higher_timeframe, period=c.slow_ema))
        return specs

    def grade(self, score: float, c: ScoringConfig | None = None) -> str:
        c = c or self.cfg
        if score >= c.strong_threshold:
            return "strong"
        if score >= c.moderate_threshold:
            return "moderate"
        return "weak"

    def score(self, signal: Signal, ctx: StrategyContext) -> ScoreResult:
        c = self.cfg_for(signal.strategy)
        if signal.action == SignalAction.EXIT or not c.enabled:
            return ScoreResult(100.0, "exit" if signal.action == SignalAction.EXIT else "unscored", True, {})
        long = signal.action == SignalAction.BUY
        close = ctx.candle.close
        ema_f, ema_s = ctx.ind("ema", period=c.fast_ema), ctx.ind("ema", period=c.slow_ema)
        ema_s_prev = ctx.ind("ema", period=c.slow_ema, offset=c.trend_slope_bars)
        rsi = ctx.ind("rsi", period=c.rsi_period)
        vwap = ctx.ind("vwap")
        vr = ctx.ind("volume_ratio", period=20)

        def result(applicable: bool, passed: bool, detail: str):
            return {"applicable": applicable, "passed": bool(passed and applicable), "detail": detail}

        f: dict[str, dict] = {}
        if ema_s is not None and ema_s_prev is not None:
            rising = ema_s > ema_s_prev
            ok = (close > ema_s and rising) if long else (close < ema_s and not rising)
            f["trend"] = result(True, ok, f"close {close:.2f} vs EMA{c.slow_ema} {ema_s:.2f}, "
                                          f"slope {'up' if rising else 'down'}")
        else:
            f["trend"] = result(False, False, "insufficient data")
        if ema_f is not None and ema_s is not None:
            f["ema"] = result(True, (ema_f > ema_s) if long else (ema_f < ema_s),
                              f"EMA{c.fast_ema} {ema_f:.2f} vs EMA{c.slow_ema} {ema_s:.2f}")
        else:
            f["ema"] = result(False, False, "insufficient data")
        if rsi is not None:
            lo, hi = c.rsi_long_range if long else c.rsi_short_range
            f["rsi"] = result(True, lo <= rsi <= hi, f"RSI {rsi:.1f} in [{lo}, {hi}]?")
        else:
            f["rsi"] = result(False, False, "insufficient data")
        f["vwap"] = (result(True, close > vwap if long else close < vwap, f"close vs VWAP {vwap:.2f}")
                     if vwap is not None else result(False, False, "no volume / VWAP"))
        f["volume"] = (result(True, vr >= c.volume_ratio_min, f"volume {vr:.2f}x avg (min {c.volume_ratio_min})")
                       if vr is not None else result(False, False, "no volume"))
        pa = ctx.price_action()
        want = "bullish" if long else "bearish"
        pa_events = [e.pattern for e in pa.events if e.direction == want and e.pattern in
                     ("breakout", "breakdown", "retest", "range_breakout", "momentum_breakout", "trend_reversal")]
        pa_ok = pa.trend == ("up" if long else "down") or bool(pa_events)
        f["price_action"] = result(pa.atr is not None, pa_ok, f"structure {pa.trend}; events {pa_events or '-'}")
        pats = [p for p in ctx.candle_patterns(lookback=2) if p.direction == want and p.confidence >= 0.5]
        f["candlestick"] = result(True, bool(pats), ", ".join(p.pattern for p in pats) or "none")
        if c.weights.get("htf_trend"):
            htf = c.higher_timeframe
            h_ema = ctx.ind("ema", tf=htf, period=c.slow_ema)
            h = ctx.candles(1, htf)
            if h_ema is not None and h:
                f["htf_trend"] = result(True, h[-1].close > h_ema if long else h[-1].close < h_ema,
                                        f"{htf} close {h[-1].close:.2f} vs EMA{c.slow_ema} {h_ema:.2f}")
            else:
                f["htf_trend"] = result(False, False, f"insufficient {htf} data")

        total = got = 0.0
        for name, res in f.items():
            w = float(c.weights.get(name, 0))
            res["weight"] = w
            if w and res["applicable"]:
                total += w
                got += w if res["passed"] else 0
        score = round(100 * got / total, 1) if total else 0.0
        return ScoreResult(score, self.grade(score, c), score >= c.min_score, f)
