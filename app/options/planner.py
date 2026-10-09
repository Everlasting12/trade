"""Options planner: turns a directional view on an underlying into a concrete option trade.

    strategy signal on the underlying (BUY = bullish, SELL = bearish, with underlying stop/target)
        -> decide the option action:  BUY_CE | BUY_PE | SELL_CE | SELL_PE
        -> choose expiry (min days-to-expiry) and strike (target |delta|, liquidity filters)
        -> derive premium stop/target from the underlying stop/target via delta (+gamma), clamped
        -> emit a Signal on the option contract (sized later by the risk manager)

Action selection ("auto" style), all thresholds configurable:
- Option BUYING needs movement: trend strength ADX >= buy_min_adx and options not expensive
  (ATM IV / realised volatility <= iv_rich_ratio). Bullish -> BUY_CE, bearish -> BUY_PE.
- Option SELLING harvests premium: market ranging (ADX < sell_max_adx) or options expensive
  (IV/RV >= iv_rich_ratio). Bullish -> SELL_PE, bearish -> SELL_CE.
- Otherwise the signal grade decides: strong -> buy, else sell (if selling is allowed).

The decision is a rule set, not a prediction of profit. Every input and reason is recorded in the
signal metadata and the trade journal so the rules can be evaluated after the paper pilot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.core.models import Instrument, OrderType, Signal, SignalAction
from app.indicators.base import IndicatorSpec
from app.market_data import timeframes as tfm
from app.options.market import ChainSnapshot, OptionMarket, OptionQuote
from app.strategies.base import StrategyContext

ACTIONS = ("BUY_CE", "BUY_PE", "SELL_CE", "SELL_PE")


@dataclass
class OptionsConfig:
    style: Literal["auto", "buy_only", "sell_only"] = "auto"
    buy_min_adx: float = 22.0
    sell_max_adx: float = 20.0
    iv_rich_ratio: float = 1.25          # ATM IV / realised vol above this = options expensive
    buy_delta: float = 0.50              # target |delta| for bought options (ATM)
    sell_delta: float = 0.25             # target |delta| for sold options (OTM)
    expiry: Literal["nearest", "next", "monthly"] = "nearest"
    min_dte_buy: float = 1.0             # days; avoid buying on expiry day (theta/gamma)
    min_dte_sell: float = 0.5            # days; avoid selling into the last hours before expiry (gamma risk)
    max_spread_pct: float = 3.0
    min_oi: float = 0.0
    min_premium: float = 5.0
    buy_max_premium_loss_pct: float = 35.0   # cap on the premium stop for bought options
    sell_max_premium_loss_pct: float = 60.0  # cap on the premium rise that stops a short option
    min_premium_risk_pct: float = 8.0        # floor so stops are not inside the noise
    default_rr: float = 2.0
    chain_width: int = 15
    product: str = "MIS"
    rv_bars: int = 50
    adx_period: int = 14

    @classmethod
    def from_dict(cls, d: dict | None) -> "OptionsConfig":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (d or {}).items() if k in known})


@dataclass
class OptionPlan:
    ok: bool
    reason: str = ""
    action: str = ""
    signal: Signal | None = None
    quote: OptionQuote | None = None
    chain: ChainSnapshot | None = None
    notes: list[str] = field(default_factory=list)


def realized_vol(closes: list[float], tf: str) -> float | None:
    if len(closes) < 10:
        return None
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 5:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    bars_per_year = 252 if tf == "1d" else 252 * 22500 / tfm.seconds(tf)
    return math.sqrt(var * bars_per_year)


class OptionsPlanner:
    def __init__(self, market: OptionMarket, config: OptionsConfig | None = None):
        self.market = market
        self.cfg = config or OptionsConfig()

    def indicators(self, tf: str) -> list[IndicatorSpec]:
        return [IndicatorSpec.of("adx", tf, period=self.cfg.adx_period)]

    # ------------------------------------------------------------------ decision
    def decide(self, bullish: bool, adx: float | None, iv: float | None, rv: float | None, grade: str,
               cfg: OptionsConfig) -> tuple[str, list[str]]:
        notes = []
        ratio = (iv / rv) if (iv and rv) else None
        notes.append(f"ADX {adx:.1f}" if adx is not None else "ADX n/a")
        notes.append(f"ATM IV {100 * iv:.1f}%" if iv else "IV n/a")
        notes.append(f"realised vol {100 * rv:.1f}%" if rv else "RV n/a")
        if ratio:
            notes.append(f"IV/RV {ratio:.2f}")
        if cfg.style == "buy_only":
            buy = True
            notes.append("style buy_only")
        elif cfg.style == "sell_only":
            buy = False
            notes.append("style sell_only")
        else:
            trending = adx is not None and adx >= cfg.buy_min_adx
            ranging = adx is not None and adx < cfg.sell_max_adx
            rich = ratio is not None and ratio >= cfg.iv_rich_ratio
            if trending and not rich:
                buy = True
                notes.append(f"trending (ADX >= {cfg.buy_min_adx}) and options not expensive -> buy")
            elif rich:
                buy = False
                notes.append(f"options expensive (IV/RV >= {cfg.iv_rich_ratio}) -> sell premium")
            elif ranging:
                buy = False
                notes.append(f"ranging (ADX < {cfg.sell_max_adx}) -> sell premium")
            else:
                buy = grade == "strong"
                notes.append(f"no clear regime; {grade} signal -> {'buy' if buy else 'sell'}")
        if buy:
            return ("BUY_CE" if bullish else "BUY_PE"), notes
        return ("SELL_PE" if bullish else "SELL_CE"), notes

    def _expiry(self, underlying: str, today: date, min_dte: float, cfg: OptionsConfig, now) -> date | None:
        exps = self.market.expiries(underlying, today)
        from app.fno.greeks import year_fraction
        exps = [e for e in exps if year_fraction(now, e) * 365 >= min_dte]
        if not exps:
            return None
        if cfg.expiry == "next" and len(exps) > 1:
            return exps[1]
        if cfg.expiry == "monthly":
            m = (exps[0].year, exps[0].month)
            return max(e for e in exps if (e.year, e.month) == m)
        return exps[0]

    def _pick(self, quotes: list[OptionQuote], target_delta: float, cfg: OptionsConfig) -> tuple[OptionQuote | None, str]:
        liquid = []
        rejected = {"premium": 0, "spread": 0, "oi": 0}
        for q in quotes:
            if q.mid < cfg.min_premium:
                rejected["premium"] += 1
                continue
            sp = q.spread_pct
            if sp is not None and sp > cfg.max_spread_pct:
                rejected["spread"] += 1
                continue
            if cfg.min_oi and (q.oi or 0) < cfg.min_oi:
                rejected["oi"] += 1
                continue
            liquid.append(q)
        if not liquid:
            return None, f"no liquid contract (filtered: {rejected})"
        with_delta = [q for q in liquid if q.delta is not None]
        if with_delta:
            return min(with_delta, key=lambda q: abs(abs(q.delta) - target_delta)), ""
        return liquid[len(liquid) // 2], "greeks unavailable; picked middle strike"

    # ------------------------------------------------------------------ plan
    def plan(self, sig: Signal, ctx: StrategyContext, underlying: Instrument, grade: str,
             cfg: OptionsConfig | None = None) -> OptionPlan:
        cfg = cfg or self.cfg
        if sig.action not in (SignalAction.BUY, SignalAction.SELL):
            return OptionPlan(False, "only entry signals are planned")
        bullish = sig.action == SignalAction.BUY
        name = underlying.name or underlying.tradingsymbol
        u_entry = sig.entry_price or ctx.candle.close
        if sig.stop_loss is None:
            return OptionPlan(False, "underlying stop-loss required to plan an option trade")
        u_risk = abs(u_entry - sig.stop_loss)
        u_reward = abs(sig.target - u_entry) if sig.target is not None else cfg.default_rr * u_risk
        now = ctx.now
        adx_v = ctx.ind("adx", period=cfg.adx_period)
        adx = adx_v["adx"] if adx_v else None
        rv = realized_vol([c.close for c in ctx.candles(cfg.rv_bars)], ctx.timeframe)

        # First pass with the nearest acceptable expiry to read ATM IV, then decide buy vs sell.
        exp_any = self._expiry(name, now.date(), min(cfg.min_dte_buy, cfg.min_dte_sell), cfg, now)
        if exp_any is None:
            return OptionPlan(False, f"no option expiry for {name}")
        try:
            chain = self.market.chain(name, exp_any, u_entry, cfg.chain_width)
        except Exception as e:
            return OptionPlan(False, f"option chain unavailable: {e}")
        action, notes = self.decide(bullish, adx, chain.atm_iv, rv, grade, cfg)
        buy = action.startswith("BUY")
        min_dte = cfg.min_dte_buy if buy else cfg.min_dte_sell
        exp = self._expiry(name, now.date(), min_dte, cfg, now)
        if exp is None:
            return OptionPlan(False, f"no expiry with >= {min_dte} days left", action, notes=notes)
        if exp != chain.expiry:
            try:
                chain = self.market.chain(name, exp, u_entry, cfg.chain_width)
            except Exception as e:
                return OptionPlan(False, f"option chain unavailable: {e}", action, notes=notes)
        kind = action[-2:]
        q, why = self._pick(chain.of_type(kind), cfg.buy_delta if buy else cfg.sell_delta, cfg)
        if q is None:
            return OptionPlan(False, why, action, chain=chain, notes=notes)
        if why:
            notes.append(why)

        inst = q.instrument
        delta = abs(q.delta) if q.delta is not None else 0.5
        gamma = q.gamma or 0.0
        entry = (q.ask or q.ltp) if buy else (q.bid or q.ltp)
        adverse = delta * u_risk + 0.5 * gamma * u_risk ** 2      # premium move if the underlying hits its stop
        favourable = max(delta * u_reward - 0.5 * gamma * u_reward ** 2, delta * u_reward * 0.5)
        floor = entry * cfg.min_premium_risk_pct / 100
        if buy:
            risk = min(max(adverse, floor), entry * cfg.buy_max_premium_loss_pct / 100)
            sl, tgt = entry - risk, entry + max(favourable, risk)
        else:
            risk = min(max(adverse, floor), entry * cfg.sell_max_premium_loss_pct / 100)
            sl, tgt = entry + risk, max(entry - max(favourable, risk * 0.5), inst.tick_size * 2)
        sl, tgt = inst.round_to_tick(sl), inst.round_to_tick(tgt)
        if buy and sl <= 0:
            sl = inst.tick_size
        planned_rr = round(abs(tgt - entry) / abs(entry - sl), 2) if entry != sl else None
        dte = round(chain.days_to_expiry(), 2)
        label = {"BUY_CE": "Buy call", "BUY_PE": "Buy put", "SELL_CE": "Sell call", "SELL_PE": "Sell put"}[action]
        reasons = [f"{label} {inst.tradingsymbol} ({'bullish' if bullish else 'bearish'} view on {name})",
                   *sig.reason, *notes,
                   f"strike {inst.strike:g}, delta {q.delta:+.2f}" if q.delta is not None else f"strike {inst.strike:g}",
                   f"{dte:.1f} days to expiry"]
        opt = Signal(SignalAction.BUY if buy else SignalAction.SELL, inst.key, sig.strategy, sig.timeframe,
                     entry, sl, tgt, None, sig.confidence, reasons, sig.ts, OrderType.MARKET, sig.score,
                     sig.score_breakdown)
        opt.metadata = {
            "option_action": action, "option_type": kind, "direction": "bullish" if bullish else "bearish",
            "underlying": name, "underlying_key": underlying.key, "underlying_entry": round(u_entry, 2),
            "underlying_sl": round(sig.stop_loss, 2),
            "underlying_target": round(sig.target, 2) if sig.target is not None else None,
            "strike": inst.strike, "expiry": str(inst.expiry), "dte": dte, "iv": q.iv, "delta": q.delta,
            "gamma": q.gamma, "theta": q.theta, "atm_iv": chain.atm_iv, "realized_vol": rv, "adx": adx,
            "forward": round(chain.forward, 2), "planned_rr": planned_rr, "chain_source": chain.source,
            "strategy_reasons": list(sig.reason), "decision_notes": notes,
            "underlying_signal_id": sig.signal_id, "product": cfg.product,
        }
        return OptionPlan(True, "", action, opt, q, chain, notes)
