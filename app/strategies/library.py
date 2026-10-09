"""Built-in strategies. These are reference implementations of common ideas, NOT recommendations:
none of them is assumed profitable. Backtest and paper-trade before any live use."""

from __future__ import annotations

from app.strategies.base import Strategy, StrategyContext


def _bracket_signal(strat: Strategy, ctx: StrategyContext, long: bool, reasons: list[str],
                    stop: float | None = None):
    c = ctx.candle
    atr = ctx.ind("atr", period=strat.params["atr_period"])
    if atr is None:
        return None
    sl, tgt = strat.atr_bracket(c.close, atr, long, strat.params["atr_mult"], strat.params["rr"])
    if stop is not None:  # structure-based stop overrides the ATR stop if it is valid
        if (long and stop < c.close) or (not long and stop > c.close):
            sl = stop
            risk = abs(c.close - sl)
            tgt = c.close + strat.params["rr"] * risk if long else c.close - strat.params["rr"] * risk
    return ctx.signal("BUY" if long else "SELL", stop_loss=sl, target=tgt, reason=reasons)


_BRACKET = {"atr_period": 14, "atr_mult": 1.5, "rr": 2.0, "allow_short": True}


class EmaCrossover(Strategy):
    name = "ema_crossover"
    description = "Fast EMA crossing slow EMA; ATR stop, fixed R:R target, exit on opposite cross."
    default_params = {"fast": 20, "slow": 50, **_BRACKET}

    def indicators(self):
        p = self.params
        return [self.spec("ema", period=p["fast"]), self.spec("ema", period=p["slow"]),
                self.spec("atr", period=p["atr_period"])]

    def on_candle(self, ctx):
        p = self.params
        fast, slow = ("ema", {"period": p["fast"]}), ("ema", {"period": p["slow"]})
        up, down = ctx.crossed_above(fast, slow), ctx.crossed_below(fast, slow)
        pos = ctx.position_qty
        if pos > 0 and down:
            return ctx.exit(f"EMA{p['fast']} crossed below EMA{p['slow']}")
        if pos < 0 and up:
            return ctx.exit(f"EMA{p['fast']} crossed above EMA{p['slow']}")
        if pos == 0 and up:
            return _bracket_signal(self, ctx, True, [f"EMA{p['fast']} crossed above EMA{p['slow']}"])
        if pos == 0 and down and p["allow_short"]:
            return _bracket_signal(self, ctx, False, [f"EMA{p['fast']} crossed below EMA{p['slow']}"])
        return None


class RsiReversal(Strategy):
    name = "rsi_reversal"
    description = "RSI leaving oversold/overbought with a confirming candle; exit at RSI mid-zone."
    default_params = {"period": 14, "oversold": 30, "overbought": 70, "exit_long": 60, "exit_short": 40, **_BRACKET}

    def indicators(self):
        return [self.spec("rsi", period=self.params["period"]), self.spec("atr", period=self.params["atr_period"])]

    def on_candle(self, ctx):
        p = self.params
        r0, r1 = ctx.ind("rsi", period=p["period"]), ctx.ind("rsi", period=p["period"], offset=1)
        if r0 is None or r1 is None:
            return None
        c, pos = ctx.candle, ctx.position_qty
        if pos > 0 and r0 >= p["exit_long"]:
            return ctx.exit(f"RSI {r0:.1f} reached exit level {p['exit_long']}")
        if pos < 0 and r0 <= p["exit_short"]:
            return ctx.exit(f"RSI {r0:.1f} reached exit level {p['exit_short']}")
        if pos == 0 and r1 < p["oversold"] <= r0 and c.close > c.open:
            return _bracket_signal(self, ctx, True, [f"RSI crossed up through {p['oversold']} ({r0:.1f})",
                                                     "Bullish confirmation candle"])
        if pos == 0 and r1 > p["overbought"] >= r0 and c.close < c.open and p["allow_short"]:
            return _bracket_signal(self, ctx, False, [f"RSI crossed down through {p['overbought']} ({r0:.1f})",
                                                      "Bearish confirmation candle"])
        return None


class VwapBreakout(Strategy):
    name = "vwap_breakout"
    description = "Close crossing VWAP on above-average volume; exit on close back across VWAP."
    default_params = {"volume_period": 20, "min_volume_ratio": 1.5, **_BRACKET}

    def indicators(self):
        return [self.spec("vwap"), self.spec("volume_ratio", period=self.params["volume_period"]),
                self.spec("atr", period=self.params["atr_period"])]

    def on_candle(self, ctx):
        p = self.params
        v0, v1 = ctx.ind("vwap"), ctx.ind("vwap", offset=1)
        vr = ctx.ind("volume_ratio", period=p["volume_period"])
        if v0 is None or v1 is None:
            return None
        candles = ctx.candles(2)
        if len(candles) < 2 or candles[-1].ts.date() != candles[-2].ts.date():
            return None  # VWAP resets each session; no cross on the first candle of the day
        prev, c, pos = candles[-2], candles[-1], ctx.position_qty
        if pos > 0 and c.close < v0:
            return ctx.exit("Close back below VWAP")
        if pos < 0 and c.close > v0:
            return ctx.exit("Close back above VWAP")
        vol_ok = vr is not None and vr >= p["min_volume_ratio"]
        if pos == 0 and vol_ok and prev.close <= v1 and c.close > v0:
            return _bracket_signal(self, ctx, True, ["Close crossed above VWAP", f"Volume {vr:.1f}x average"])
        if pos == 0 and vol_ok and prev.close >= v1 and c.close < v0 and p["allow_short"]:
            return _bracket_signal(self, ctx, False, ["Close crossed below VWAP", f"Volume {vr:.1f}x average"])
        return None


class SupertrendStrategy(Strategy):
    name = "supertrend"
    description = "Supertrend direction flip; stop at the Supertrend line; exit on opposite flip."
    default_params = {"period": 10, "multiplier": 3.0, **_BRACKET}

    def indicators(self):
        p = self.params
        return [self.spec("supertrend", period=p["period"], multiplier=p["multiplier"]),
                self.spec("atr", period=p["atr_period"])]

    def on_candle(self, ctx):
        p = self.params
        kw = {"period": p["period"], "multiplier": p["multiplier"]}
        s0, s1 = ctx.ind("supertrend", **kw), ctx.ind("supertrend", offset=1, **kw)
        if not s0 or not s1:
            return None
        flip_up = s1["direction"] == -1 and s0["direction"] == 1
        flip_dn = s1["direction"] == 1 and s0["direction"] == -1
        pos = ctx.position_qty
        if pos > 0 and flip_dn:
            return ctx.exit("Supertrend flipped bearish")
        if pos < 0 and flip_up:
            return ctx.exit("Supertrend flipped bullish")
        if pos == 0 and flip_up:
            return _bracket_signal(self, ctx, True, ["Supertrend flipped bullish"], stop=s0["value"])
        if pos == 0 and flip_dn and p["allow_short"]:
            return _bracket_signal(self, ctx, False, ["Supertrend flipped bearish"], stop=s0["value"])
        return None


class BreakoutStrategy(Strategy):
    name = "sr_breakout"
    description = "Support/resistance, range and momentum breakouts from price-action analysis."
    default_params = {"min_confidence": 0.6, "require_trend_alignment": False, **_BRACKET}

    def indicators(self):
        return [self.spec("atr", period=self.params["atr_period"])]

    def on_candle(self, ctx):
        if ctx.position_qty != 0:
            return None  # exits handled by the bracket (stop/target) placed by the OMS
        p = self.params
        pa = ctx.price_action()
        if pa.atr is None:
            return None
        for ev in pa.events:
            if ev.pattern not in ("breakout", "breakdown", "range_breakout", "momentum_breakout"):
                continue
            if ev.confidence < p["min_confidence"]:
                continue
            long = ev.direction == "bullish"
            if not long and not p["allow_short"]:
                continue
            if p["require_trend_alignment"] and pa.trend != ("up" if long else "down"):
                continue
            d = ev.details
            level = d.get("level") or (d.get("range_low") if long else d.get("range_high"))
            stop = (level - 0.5 * pa.atr if long else level + 0.5 * pa.atr) if level else None
            reasons = [f"{ev.pattern.replace('_', ' ')} ({ev.direction}, conf {ev.confidence:.2f})",
                       f"Market structure: {pa.trend}"]
            if d.get("volume_ratio"):
                reasons.append(f"Volume {d['volume_ratio']}x average")
            sig = _bracket_signal(self, ctx, long, reasons, stop=stop)
            if sig:
                sig.metadata["pattern"] = ev.as_dict()
            return sig
        return None


class MultiConfirmation(Strategy):
    name = "multi_confirmation"
    description = "Enter when EMA trend, RSI, VWAP, volume and ADX all agree (on the transition)."
    default_params = {"fast": 20, "slow": 50, "rsi_period": 14, "rsi_long": 55, "rsi_short": 45,
                      "adx_min": 20, "min_volume_ratio": 1.2, **_BRACKET}

    def indicators(self):
        p = self.params
        return [self.spec("ema", period=p["fast"]), self.spec("ema", period=p["slow"]),
                self.spec("rsi", period=p["rsi_period"]), self.spec("vwap"), self.spec("adx", period=14),
                self.spec("volume_ratio", period=20), self.spec("atr", period=p["atr_period"])]

    def _state(self, ctx, offset: int) -> tuple[bool, bool, list[str]] | None:
        p = self.params
        f, s = ctx.ind("ema", period=p["fast"], offset=offset), ctx.ind("ema", period=p["slow"], offset=offset)
        r = ctx.ind("rsi", period=p["rsi_period"], offset=offset)
        vw = ctx.ind("vwap", offset=offset)
        adx = ctx.ind("adx", period=14, offset=offset)
        vr = ctx.ind("volume_ratio", period=20, offset=offset)
        candles = ctx.candles(offset + 1)
        if None in (f, s, r, adx) or len(candles) < offset + 1:
            return None
        close = candles[-1 - offset].close
        vol_ok = vr is None or vr >= p["min_volume_ratio"]
        vw_long = vw is None or close > vw   # indices have no volume -> VWAP not applicable
        vw_short = vw is None or close < vw
        trending = adx["adx"] >= p["adx_min"]
        long = f > s and r >= p["rsi_long"] and vw_long and vol_ok and trending
        short = f < s and r <= p["rsi_short"] and vw_short and vol_ok and trending
        reasons = [f"EMA{p['fast']} {'>' if f > s else '<'} EMA{p['slow']}", f"RSI {r:.1f}",
                   f"ADX {adx['adx']:.1f}"]
        if vw is not None:
            reasons.append(f"Close {'above' if close > vw else 'below'} VWAP")
        if vr is not None:
            reasons.append(f"Volume {vr:.1f}x average")
        return long, short, reasons

    def on_candle(self, ctx):
        now, before = self._state(ctx, 0), self._state(ctx, 1)
        if now is None or before is None:
            return None
        long, short, reasons = now
        pos = ctx.position_qty
        f, s = ctx.ind("ema", period=self.params["fast"]), ctx.ind("ema", period=self.params["slow"])
        if pos > 0 and f < s:
            return ctx.exit("EMA trend turned down")
        if pos < 0 and f > s:
            return ctx.exit("EMA trend turned up")
        if pos == 0 and long and not before[0]:
            return _bracket_signal(self, ctx, True, reasons)
        if pos == 0 and short and not before[1] and self.params["allow_short"]:
            return _bracket_signal(self, ctx, False, reasons)
        return None


STRATEGIES: dict[str, type[Strategy]] = {cls.name: cls for cls in (
    EmaCrossover, RsiReversal, VwapBreakout, SupertrendStrategy, BreakoutStrategy, MultiConfirmation,
)}


def register(cls: type[Strategy]) -> type[Strategy]:
    """Decorator for custom user-defined strategies: `@register class MyStrat(Strategy): name = "my_strat"`."""
    STRATEGIES[cls.name] = cls
    return cls


def create(type_name: str, **kwargs) -> Strategy:
    try:
        return STRATEGIES[type_name](**kwargs)
    except KeyError:
        raise ValueError(f"Unknown strategy type {type_name!r}. Available: {sorted(STRATEGIES)}") from None
