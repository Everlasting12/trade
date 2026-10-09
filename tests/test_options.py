from datetime import date, datetime

import pytest

from app.backtesting.engine import BacktestConfig, run_backtest
from app.core.clock import IST, SimClock
from app.core.models import Instrument, OrderRole, OrderStatus, OrderType, Product, Signal, SignalAction
from app.market_data.historical.service import synthetic_candles
from app.market_data.instruments import InstrumentRegistry
from app.options.market import ModelOptionMarket, implied_forward
from app.options.planner import OptionsConfig, OptionsPlanner, realized_vol
from app.risk.manager import RiskConfig
from app.signals.scoring import ScoringConfig
from app.strategies.library import STRATEGIES

FUT = Instrument(1001, "NFO", "NIFTYTESTFUT", "FUT", "NFO-FUT", "NIFTY", 75, 0.05, date(2027, 1, 1))


def _cfg(style="auto", **opt):
    return BacktestConfig(
        capital=2_000_000,
        risk=RiskConfig(max_risk_per_trade=20_000, max_daily_loss=200_000, max_trades_per_day=20,
                        max_consecutive_losses=99, max_open_positions=5, allow_option_selling=True,
                        max_capital_utilization=0.95),
        scoring=ScoringConfig(enabled=False), options=OptionsConfig(style=style, **opt))


def _run(style, seed=11, days=8, strategy="ema_crossover", **opt):
    bars = synthetic_candles(date(2026, 1, 5), days, "1m", 25000, seed=seed)
    s = STRATEGIES[strategy]([FUT.key], timeframe="5m", execution="options")
    return run_backtest([s], {FUT.key: bars}, InstrumentRegistry([FUT]), _cfg(style, **opt))


@pytest.mark.parametrize("style,allowed", [
    ("buy_only", {"BUY_CE", "BUY_PE"}),
    ("sell_only", {"SELL_CE", "SELL_PE"}),
    ("auto", {"BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE"}),
])
def test_options_backtest_trades_contracts_and_balances(style, allowed):
    res = _run(style)
    assert not [w for w in res.warnings if w["event"] in ("strategy_error", "scoring_error", "engine_error")]
    approved = [s for s in res.signals if s["status"] == "approved"]
    assert approved, f"no approved option signals: {[s['status'] for s in res.signals][:10]}"
    actions = {s["metadata"]["option_action"] for s in approved}
    assert actions <= allowed
    assert res.trades, "no option trades closed"
    for t in res.trades:
        assert t["instrument"].endswith(("CE", "PE"))      # traded the option, not the future
        assert t["quantity"] % 75 == 0
        assert t["entry_ts"].date() == t["exit_ts"].date()  # MIS: closed intraday
    m = res.metrics
    assert m["net_pnl"] == pytest.approx(sum(t["net_pnl"] for t in res.trades), abs=0.05 * max(1, m["num_trades"]))


def test_direction_maps_to_option_side():
    res = _run("auto")
    for s in res.signals:
        meta = s.get("metadata") or {}
        if "option_action" in meta and s["status"] == "approved":
            bull = meta["direction"] == "bullish"
            assert meta["option_action"] in (("BUY_CE", "SELL_PE") if bull else ("BUY_PE", "SELL_CE"))
            # Premium levels on the right side: long option stops below entry, short option stops above.
            if meta["option_action"].startswith("BUY"):
                assert s["stop_loss"] < s["entry_price"] < s["target"]
            else:
                assert s["target"] < s["entry_price"] < s["stop_loss"]


def test_option_selling_blocked_when_disabled():
    cfg = _cfg("sell_only")
    cfg.risk.allow_option_selling = False
    bars = synthetic_candles(date(2026, 1, 5), 5, "1m", 25000, seed=11)
    s = STRATEGIES["ema_crossover"]([FUT.key], timeframe="5m", execution="options")
    res = run_backtest([s], {FUT.key: bars}, InstrumentRegistry([FUT]), cfg)
    assert res.metrics["num_trades"] == 0
    assert any("option_selling" in r for s in res.signals for r in (s.get("risk") or {}).get("reasons", []))


def test_option_stop_is_stop_limit():
    res = _run("buy_only")
    stops = [o for o in res.orders if o["role"] == "STOP_LOSS"]
    assert stops and all(o["order_type"] == "SL" and o["price"] is not None for o in stops)
    for o in stops:  # sell stop-limit: limit below trigger
        assert o["price"] < o["trigger_price"]


# ---------------------------------------------------------------- planner unit tests

class _Ctx:
    """Minimal StrategyContext stand-in for the planner."""
    def __init__(self, now, closes, adx):
        self.now, self.timeframe, self._closes, self._adx = now, "5m", closes, adx

    def ind(self, name, **kw):
        return {"adx": self._adx} if name == "adx" else None

    def candles(self, n=None, tf=None):
        from types import SimpleNamespace
        return [SimpleNamespace(close=c) for c in self._closes[-(n or len(self._closes)):]]

    @property
    def candle(self):
        from types import SimpleNamespace
        return SimpleNamespace(close=self._closes[-1])


def _planner(price=25000.0, iv=0.14, now=datetime(2026, 1, 5, 11, 0, tzinfo=IST)):
    reg = InstrumentRegistry([FUT])
    m = ModelOptionMarket(reg, lambda n: price, SimClock(now), iv=iv)
    return OptionsPlanner(m), now


def _sig(action, entry=25000.0, sl=24950.0, tgt=25100.0):
    return Signal(SignalAction(action), FUT.key, "s", "5m", entry, sl, tgt)


def test_planner_trending_buys_atm_and_ranging_sells_otm():
    pl, now = _planner()
    closes = [25000 + (i % 3) for i in range(60)]   # low realised vol
    trend = pl.plan(_sig("BUY"), _Ctx(now, closes, adx=35), FUT, "moderate", OptionsConfig(iv_rich_ratio=99))
    assert trend.ok and trend.action == "BUY_CE"
    assert abs(abs(trend.quote.delta) - 0.5) < 0.12
    rng = pl.plan(_sig("SELL", sl=25050, tgt=24900), _Ctx(now, closes, adx=12), FUT, "moderate", OptionsConfig(iv_rich_ratio=99))
    assert rng.ok and rng.action == "SELL_CE"
    assert abs(rng.quote.delta) < 0.4 and rng.quote.strike > 25000      # OTM call
    md = rng.signal.metadata
    assert md["underlying_sl"] == 25050 and md["direction"] == "bearish" and md["planned_rr"] > 0


def test_planner_respects_min_dte_and_expensive_iv():
    pl, now = _planner(now=datetime(2026, 1, 6, 11, 0, tzinfo=IST))  # a Tuesday = model expiry day
    closes = [25000 * (1 + 0.0002 * (-1) ** i) for i in range(60)]
    p = pl.plan(_sig("BUY"), _Ctx(now, closes, adx=35), FUT, "strong", OptionsConfig(style="buy_only"))
    assert p.ok and p.action == "BUY_CE"
    assert p.quote.instrument.expiry > now.date()                       # skipped same-day expiry for a buy
    # IV (14%) far above realised vol -> options expensive -> sell premium, and never into today's expiry
    p = pl.plan(_sig("BUY"), _Ctx(now, closes, adx=35), FUT, "strong", OptionsConfig())
    assert p.ok and p.action == "SELL_PE" and "expensive" in " ".join(p.notes)
    assert p.quote.instrument.expiry > now.date()


def test_realized_vol_and_parity_forward():
    assert realized_vol([100.0] * 20, "5m") == 0.0
    assert realized_vol([100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100], "1d") > 0.1
    from app.options.market import OptionQuote
    k = Instrument(1, "NFO", "X", "CE", "", "NIFTY", 75, 0.05, date(2026, 1, 13), 25000.0)
    pk = Instrument(2, "NFO", "Y", "PE", "", "NIFTY", 75, 0.05, date(2026, 1, 13), 25000.0)
    f = implied_forward([OptionQuote(k, 120, 119, 121), OptionQuote(pk, 100, 99, 101)], 1.0)
    assert f == pytest.approx(25020.0)


def test_journal_partial_exits_and_r_multiple(tmp_path):
    from app.database.repository import Repository
    repo = Repository(tmp_path / "j.db", "paper")
    repo.journal_open({"position_id": "p1", "strategy": "s", "instrument": "NFO:X", "action": "BUY_CE",
                       "quantity": 150, "entry_time": "2026-01-05T10:00:00+05:30", "entry_price": 100.0,
                       "stop_loss": 70.0, "planned_risk": 4500.0, "reasons": ["a", "b"]})
    repo.journal_close("p1", {"exit_time": "2026-01-05T10:30:00+05:30", "exit_price": 120.0, "exit_quantity": 75,
                              "exit_reason": "target", "gross_pnl": 1500.0, "charges": 40.0, "net_pnl": 1460.0,
                              "closed": False})
    r = repo.journal()[0]
    assert r["status"] == "open" and r["exit_quantity"] == 75
    repo.journal_close("p1", {"exit_time": "2026-01-05T11:00:00+05:30", "exit_price": 90.0, "exit_quantity": 75,
                              "exit_reason": "underlying stop", "gross_pnl": -750.0, "charges": 40.0,
                              "net_pnl": -790.0, "closed": True})
    r = repo.journal()[0]
    assert r["status"] == "closed" and r["exit_price"] == pytest.approx(105.0)
    assert r["net_pnl"] == pytest.approx(670.0) and r["r_multiple"] == pytest.approx(670 / 4500, abs=0.01)
    assert r["reasons"] == ["a", "b"]


def test_backtest_journal_records_option_trade_details():
    res = _run("auto")
    closed = [j for j in res.journal if j["status"] == "closed"]
    assert closed
    j = closed[0]
    for k in ("action", "strike", "expiry", "entry_time", "entry_price", "stop_loss", "target", "planned_rr",
              "underlying_entry", "underlying_sl", "exit_time", "exit_price", "exit_reason", "net_pnl", "r_multiple"):
        assert j.get(k) is not None, k
    assert j["action"] in ("BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE")
    assert sum(x["net_pnl"] for x in closed) == pytest.approx(res.metrics["net_pnl"], abs=1.0 * len(closed))


def test_watchlist_repository_and_search_types(tmp_path):
    from app.database.repository import Repository
    repo = Repository(tmp_path / "w.db", "paper")
    a = repo.create_watchlist("Banks")
    b = repo.create_watchlist("F&O")
    item = {"key": "NSE:HDFCBANK", "token": 1, "exchange": "NSE", "tradingsymbol": "HDFCBANK", "name": "HDFC BANK",
            "instrument_type": "EQ", "segment": "NSE", "lot_size": 1}
    assert repo.add_watchlist_items(a, [item]) == 1
    assert repo.add_watchlist_items(a, [item]) == 0          # duplicates ignored
    repo.add_watchlist_items(b, [item])
    assert {w["name"]: w["count"] for w in repo.watchlists()} == {"Banks": 1, "F&O": 1}
    repo.remove_watchlist_items(a, ["NSE:HDFCBANK"])
    assert repo.watchlist_items(a) == [] and repo.all_watchlist_tokens() == [1]
    repo.delete_watchlist(b)
    assert [w["name"] for w in repo.watchlists()] == ["Banks"]

    reg = InstrumentRegistry([FUT, Instrument(5, "NFO", "NIFTY26JAN25000CE", "CE", "NFO-OPT", "NIFTY", 75, 0.05,
                                              date(2026, 1, 27), 25000.0)])
    assert [i.key for i in reg.search("NIFTY", types=("FUT",))] == [FUT.key]
    assert len(reg.search("NIFTY")) == 2
