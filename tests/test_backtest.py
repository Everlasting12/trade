from datetime import date

import pytest

from app.backtesting.engine import BacktestConfig, run_backtest
from app.market_data.historical.service import synthetic_candles
from app.market_data.instruments import InstrumentRegistry
from app.risk.manager import RiskConfig
from app.signals.scoring import ScoringConfig
from app.strategies.base import Strategy
from app.strategies.library import STRATEGIES


class ProbeStrategy(Strategy):
    """Buys once, records what it could see, and asserts no future data is visible."""
    name = "probe"
    seen: list = []

    def indicators(self):
        return [self.spec("atr", period=14)]

    def on_candle(self, ctx):
        series = ctx.candles()
        assert series[-1].ts < ctx.now, "strategy saw an unfinished candle"
        assert all(c.ts < ctx.now for c in series)
        ProbeStrategy.seen.append((ctx.now, series[-1].ts))
        if ctx.position_qty == 0 and len(ProbeStrategy.seen) == 30:
            c = ctx.candle
            return ctx.signal("BUY", stop_loss=c.close - 30, target=c.close + 60, reason=["probe"])
        return None


def _cfg(**kw):
    return BacktestConfig(capital=2_000_000,
                          risk=RiskConfig(max_risk_per_trade=50_000, max_daily_loss=200_000, max_trades_per_day=50,
                                          max_consecutive_losses=99, max_open_positions=5),
                          scoring=ScoringConfig(enabled=False), **kw)


def test_no_lookahead_and_next_bar_fill(nifty_fut):
    ProbeStrategy.seen = []
    bars = synthetic_candles(date(2026, 1, 5), 2, "1m", seed=3)
    reg = InstrumentRegistry([nifty_fut])
    s = ProbeStrategy([nifty_fut.key], timeframe="5m", params={"warmup_bars": 1})
    cfg = _cfg()
    cfg.paper.slippage_bps = 0
    cfg.paper.partial_fills = False
    res = run_backtest([s], {nifty_fut.key: bars}, reg, cfg)
    entry = next(o for o in res.orders if o["role"] == "ENTRY")
    signal_time = ProbeStrategy.seen[29][0]
    next_bar = next(b for b in bars if b.ts >= signal_time)
    assert entry["avg_fill_price"] == pytest.approx(nifty_fut.round_to_tick(next_bar.open))


@pytest.mark.parametrize("name", sorted(STRATEGIES))
def test_every_builtin_strategy_runs_and_books_balance(name, nifty_fut):
    bars = synthetic_candles(date(2026, 1, 5), 8, "1m", seed=11)
    reg = InstrumentRegistry([nifty_fut])
    s = STRATEGIES[name]([nifty_fut.key], timeframe="5m")
    res = run_backtest([s], {nifty_fut.key: bars}, reg, _cfg())
    assert res.bars_processed == len(bars)
    assert not [w for w in res.warnings if w["event"] in ("strategy_error", "scoring_error")]
    m = res.metrics
    # Accounting identity: all positions are closed at the end, so equity change == sum of trade net P&L.
    assert m["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in res.trades), abs=0.05 * max(1, m["num_trades"]))
    for t in res.trades:  # intraday product: nothing is carried overnight
        assert t["entry_ts"].date() == t["exit_ts"].date()
    assert set(m) >= {"total_return_pct", "cagr_pct", "win_rate_pct", "profit_factor", "max_drawdown",
                      "sharpe", "avg_win", "avg_loss", "max_consecutive_wins", "max_consecutive_losses"}


def test_scoring_and_risk_filter_signals(nifty_fut):
    bars = synthetic_candles(date(2026, 1, 5), 6, "1m", seed=5)
    reg = InstrumentRegistry([nifty_fut])
    cfg = _cfg()
    cfg.scoring = ScoringConfig(min_score=101)  # nothing can pass
    res = run_backtest([STRATEGIES["ema_crossover"]([nifty_fut.key], timeframe="5m")], {nifty_fut.key: bars}, reg, cfg)
    entries = [s for s in res.signals if s["action"] != "EXIT"]
    assert entries and all(s["status"] == "ignored_low_score" for s in entries)
    assert res.metrics["num_trades"] == 0


def test_daily_timeframe_backtest(nifty_fut):
    from app.core.models import Product
    bars = synthetic_candles(date(2025, 1, 1), 300, "1d", seed=9)
    reg = InstrumentRegistry([nifty_fut])
    cfg = _cfg(base_timeframe="1d", product=Product.NRML)
    cfg.risk.max_risk_per_trade, cfg.risk.max_stop_distance_pct = 150_000, 10.0
    res = run_backtest([STRATEGIES["ema_crossover"]([nifty_fut.key], timeframe="1d",
                                                    params={"fast": 10, "slow": 30})], {nifty_fut.key: bars}, reg, cfg)
    assert res.metrics["num_trades"] > 0
    assert any(t["entry_ts"].date() != t["exit_ts"].date() for t in res.trades)  # positions carried
