"""Backtests run the production engine on historical candles (Kite, CSV) or synthetic data."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.backtesting.engine import BacktestConfig, run_backtest
from app.config import factory
from app.config.settings import load_trading_config
from app.core.clock import IST
from app.core.models import Instrument, Product
from app.market_data import timeframes as tfm
from app.market_data.historical.service import HistoricalDataService, candles_from_frame, synthetic_candles
from app.market_data.instruments import InstrumentRegistry
from app.risk.manager import RiskConfig
from app.signals.scoring import ScoringConfig
from app.strategies.library import STRATEGIES
from backend.deps import container

router = APIRouter(prefix="/backtest", tags=["backtest"])


@router.get("/strategies")
def strategies():
    return [{"type": name, "description": cls.description, "default_params": cls.default_params}
            for name, cls in sorted(STRATEGIES.items())]


class BacktestRequest(BaseModel):
    strategy_type: str
    params: dict[str, Any] = {}
    timeframe: str = "5m"
    base_timeframe: str = "1m"
    source: Literal["kite", "synthetic", "csv"] = "synthetic"
    instrument: str | None = None          # kite: key or symbolic spec (e.g. FUT:NIFTY)
    from_date: date | None = None
    to_date: date | None = None
    continuous: bool = False               # kite day candles of rolled futures
    csv_rows: list[dict] | None = None     # csv: records with date, open, high, low, close[, volume, oi]
    lot_size: int = 75                     # csv/synthetic instrument
    tick_size: float = 0.05
    synthetic_days: int = 20
    synthetic_price: float = 25000
    synthetic_seed: int = 42
    capital: float = 500_000
    product: Literal["MIS", "NRML"] = "MIS"
    slippage_bps: float = 2.0
    partial_fills: bool = True
    execution: Literal["underlying", "options"] = "underlying"
    options: dict[str, Any] = Field(default_factory=dict)    # overrides of the configured options planner
    option_iv: float = 0.14                                   # model IV for option pricing in backtests
    risk: dict[str, Any] = Field(default_factory=dict)       # overrides of the configured risk limits
    scoring: dict[str, Any] | None = None                     # overrides of the configured scoring
    enforce_session_times: bool = True


@router.post("/run")
def run(req: BacktestRequest):
    c = container()
    if req.strategy_type not in STRATEGIES:
        raise HTTPException(400, f"Unknown strategy {req.strategy_type}")
    tfm.validate(req.timeframe)
    tfm.validate(req.base_timeframe)
    if tfm.seconds(req.timeframe) < tfm.seconds(req.base_timeframe):
        raise HTTPException(400, "timeframe must be >= base_timeframe")

    if req.source == "kite":
        if not req.instrument:
            raise HTTPException(400, "instrument required for kite source")
        reg = c.instruments.registry()
        try:
            inst = reg.resolve(req.instrument)
        except KeyError:
            raise HTTPException(404, f"Unknown instrument {req.instrument}")
        if not inst.tradable:
            raise HTTPException(400, "Indices are not tradable; backtest the index future instead")
        end = req.to_date or date.today()
        start = req.from_date or end - timedelta(days=30)
        hist = HistoricalDataService(c.session.kite, c.repo)
        try:
            if req.continuous:
                if req.base_timeframe != "1d":
                    raise HTTPException(400, "continuous futures data is only available for day candles")
                rows = c.session.kite.historical_data(inst.token, start, end, "day", continuous=True, oi=True)
                bars = candles_from_frame(pd.DataFrame(rows), "1d")
            else:
                bars = hist.fetch(inst.token, req.base_timeframe, datetime.combine(start, datetime.min.time(), IST),
                                  datetime.combine(end, datetime.max.time(), IST).replace(microsecond=0),
                                  oi=inst.is_derivative)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Historical data unavailable (needs the Kite historical add-on): {e}")
        reg_bt = InstrumentRegistry([inst])
    else:
        inst = Instrument(1, "NFO", "BACKTEST-FUT" if req.source == "synthetic" else "CSV-INSTRUMENT", "FUT",
                          "NFO-FUT", "BT", req.lot_size, req.tick_size)
        if req.execution == "options":
            inst = Instrument(1, "NFO", inst.tradingsymbol, "FUT", "NFO-FUT", "NIFTY", req.lot_size, req.tick_size)
        reg_bt = InstrumentRegistry([inst])
        if req.source == "synthetic":
            bars = synthetic_candles(req.from_date or date(2026, 1, 5), req.synthetic_days, req.base_timeframe,
                                     req.synthetic_price, seed=req.synthetic_seed)
        else:
            if not req.csv_rows:
                raise HTTPException(400, "csv_rows required for csv source")
            try:
                bars = candles_from_frame(pd.DataFrame(req.csv_rows), req.base_timeframe)
            except ValueError as e:
                raise HTTPException(400, str(e))
    if len(bars) < 50:
        raise HTTPException(400, f"Only {len(bars)} candles; need more data")

    cfg_file = load_trading_config(c.settings.trading_config)
    risk = factory.risk_config(cfg_file, c.settings)
    for k, v in req.risk.items():
        if hasattr(risk, k):
            setattr(risk, k, v)
    risk.max_stop_distance_pct = req.risk.get("max_stop_distance_pct", risk.max_stop_distance_pct)
    scoring, _ = factory.scoring_configs(cfg_file)
    if req.scoring is not None:
        base = {**(cfg_file.get("scoring") or {}), **req.scoring}
        base["weights"] = {**((cfg_file.get("scoring") or {}).get("weights") or {}), **(req.scoring.get("weights") or {})}
        scoring = ScoringConfig.from_dict(base)
    paper = factory.paper_config(cfg_file, req.capital)
    paper.slippage_bps, paper.partial_fills = req.slippage_bps, req.partial_fills
    product = Product.NRML if req.base_timeframe == "1d" else Product(req.product)
    strategy = STRATEGIES[req.strategy_type]([inst.key], timeframe=req.timeframe, params=req.params,
                                             strategy_id=req.strategy_type, execution=req.execution)
    opt_base, _ = factory.options_configs(cfg_file)
    from dataclasses import asdict
    from app.options.planner import OptionsConfig
    opts = OptionsConfig.from_dict({**asdict(opt_base), **req.options})
    cfg = BacktestConfig(capital=req.capital, base_timeframe=req.base_timeframe, product=product, paper=paper,
                         risk=risk, scoring=scoring, enforce_session_times=req.enforce_session_times,
                         options=opts, option_iv=req.option_iv)
    res = run_backtest([strategy], {inst.key: bars}, reg_bt, cfg)
    return {
        "instrument": inst.key, "lot_size": inst.lot_size, "bars": res.bars_processed,
        "period": [bars[0].ts, bars[-1].ts], "source": req.source, "metrics": res.metrics, "trades": res.trades,
        "journal": res.journal, "candles": res.candles, "timeframe": req.timeframe,
        "equity_curve": res.equity_curve, "signals": res.signals[:2000], "strategies": res.strategies,
        "warnings": res.warnings[:200], "risk_config": vars(risk) | {"strategy_limits": {}},
        "execution": req.execution,
        "disclaimer": ("Synthetic random-walk data: results say nothing about real performance."
                       if req.source == "synthetic" else
                       "Past performance on historical data does not guarantee future results.")
                      + (" Option prices in backtests are MODELLED (Black-76, fixed IV with a smile), not real "
                         "historical premiums." if req.execution == "options" else ""),
    }
