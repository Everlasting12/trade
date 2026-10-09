"""Per-instrument indicator state, updated incrementally on each closed candle."""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from app.core.models import Candle
from app.indicators.base import Indicator, IndicatorSpec
from app.indicators.library import create


class IndicatorEngine:
    """Holds indicator instances for one instrument across timeframes, plus a bounded history of
    their values aligned with the candle series (so strategies can look at `offset=1` etc.)."""

    def __init__(self, history: int = 500):
        self._history_len = history
        self._ind: dict[IndicatorSpec, Indicator] = {}
        self._hist: dict[IndicatorSpec, deque] = {}
        self._by_tf: dict[str, list[IndicatorSpec]] = {}

    def register(self, spec: IndicatorSpec, warmup: Iterable[Candle] = ()) -> None:
        """Add an indicator; `warmup` = already-closed candles of spec.timeframe to replay."""
        if spec in self._ind:
            return
        if not spec.timeframe:
            raise ValueError(f"IndicatorSpec {spec} needs a timeframe")
        ind = create(spec.name, **spec.kwargs)
        hist: deque = deque(maxlen=self._history_len)
        for c in warmup:
            hist.append(ind.update(c))
        self._ind[spec], self._hist[spec] = ind, hist
        self._by_tf.setdefault(spec.timeframe, []).append(spec)

    def on_candle(self, candle: Candle) -> dict[IndicatorSpec, Any]:
        out = {}
        for spec in self._by_tf.get(candle.timeframe, ()):
            v = self._ind[spec].update(candle)
            self._hist[spec].append(v)
            out[spec] = v
        return out

    def value(self, spec: IndicatorSpec, offset: int = 0) -> Any:
        """offset=0 -> value at the latest closed candle, 1 -> the one before, ..."""
        h = self._hist.get(spec)
        if h is None:
            raise KeyError(f"Indicator {spec.key} not registered (declare it in Strategy.indicators())")
        if offset >= len(h):
            return None
        return h[-1 - offset]

    def series(self, spec: IndicatorSpec, n: int | None = None) -> list[Any]:
        h = list(self._hist.get(spec, ()))
        return h if n is None else h[-n:]

    @property
    def specs(self) -> list[IndicatorSpec]:
        return list(self._ind)


def compute_frame(candles: list[Candle], specs: Iterable[IndicatorSpec]):
    """Batch helper for charts/analysis: returns a DataFrame of candles + flattened indicator columns."""
    import pandas as pd

    df = pd.DataFrame([c.as_dict() for c in candles])
    for spec in specs:
        ind = create(spec.name, **spec.kwargs)
        vals = [ind.update(c) for c in candles]
        label = spec.key.split("@")[0]
        if any(isinstance(v, dict) for v in vals):
            keys = next(v for v in vals if isinstance(v, dict)).keys()
            for k in keys:
                df[f"{label}.{k}"] = [v[k] if isinstance(v, dict) else None for v in vals]
        else:
            df[label] = vals
    return df
