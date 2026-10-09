"""Kite-style candlestick charts with TradingView Lightweight Charts (streamlit-lightweight-charts).

Panes (time-synced): price (candles + overlay indicators + trade markers), volume, and one optional
oscillator. Times are shifted to IST so the axis reads like Kite.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit_lightweight_charts import renderLightweightCharts

IST_OFFSET = 19800  # lightweight-charts renders UTC; shift epoch seconds so the axis shows IST

UP, DOWN = "#26a69a", "#ef5350"
LINE_COLORS = ["#2962FF", "#FF6D00", "#9C27B0", "#00897B", "#795548", "#C2185B", "#546E7A"]
OSCILLATORS = {"rsi", "macd", "atr", "adx", "stoch", "cci", "obv", "volume_ratio"}
BULLISH_ACTIONS = {"BUY_CE", "SELL_PE", "BUY"}


def _dark() -> bool:
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


def _base(height: int, watermark: str = "") -> dict:
    dark = _dark()
    bg, fg, grid = ("#131722", "#d1d4dc", "#1e222d") if dark else ("#ffffff", "#333333", "#f0f3fa")
    return {
        "height": height,
        "layout": {"background": {"type": "solid", "color": bg}, "textColor": fg, "fontSize": 12},
        "grid": {"vertLines": {"color": grid}, "horzLines": {"color": grid}},
        "crosshair": {"mode": 0},
        "rightPriceScale": {"borderColor": grid, "scaleMargins": {"top": 0.08, "bottom": 0.08}},
        "timeScale": {"borderColor": grid, "timeVisible": True, "secondsVisible": False, "rightOffset": 6,
                      "barSpacing": 8, "minBarSpacing": 2},
        "watermark": {"visible": bool(watermark), "fontSize": 40, "horzAlign": "center", "vertAlign": "center",
                      "color": "rgba(120,120,120,0.10)", "text": watermark},
        "handleScroll": True, "handleScale": True,
    }


def _epoch(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Kolkata")
    return int(t.timestamp()) + IST_OFFSET


def _snap(times: list[int], t: int) -> int | None:
    """Markers must sit on an existing bar: use the bar that contains time t."""
    lo, hi = 0, len(times) - 1
    if not times or t < times[0]:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if times[mid] <= t:
            lo = mid
        else:
            hi = mid - 1
    return times[lo]


def trade_markers(markers: list[dict], times: list[int]) -> list[dict]:
    out = []
    for m in markers or []:
        t = _snap(times, _epoch(m["time"]))
        if t is None:
            continue
        bull = (m.get("action") or "") in BULLISH_ACTIONS
        if m["kind"] == "entry":
            out.append({"time": t, "position": "belowBar" if bull else "aboveBar",
                        "shape": "arrowUp" if bull else "arrowDown", "color": UP if bull else DOWN,
                        "text": m.get("label", "")})
        else:
            pnl = m.get("pnl") or 0
            out.append({"time": t, "position": "aboveBar" if bull else "belowBar", "shape": "circle",
                        "color": UP if pnl >= 0 else DOWN, "text": m.get("label", "exit")})
    return sorted(out, key=lambda x: x["time"])


def candle_chart(candles: list[dict], *, title: str = "", markers: list[dict] | None = None,
                 height: int = 440, key: str = "chart") -> None:
    """`candles`: dicts with ts, open, high, low, close, volume and optional indicator columns such as
    "ema(period=20)", "rsi(period=14)", "bbands(mult=2,period=20).upper", "supertrend(...).value"."""
    if not candles:
        st.info("No candles to show yet.", icon=":material/candlestick_chart:")
        return
    df = pd.DataFrame(candles)
    df["time"] = df["ts"].map(_epoch)
    df = df.drop_duplicates("time").sort_values("time")
    times = df["time"].tolist()
    price = [{"time": r.time, "open": r.open, "high": r.high, "low": r.low, "close": r.close} for r in df.itertuples()]
    series = [{"type": "Candlestick", "data": price, "markers": trade_markers(markers, times),
               "options": {"upColor": UP, "downColor": DOWN, "borderVisible": False, "wickUpColor": UP,
                           "wickDownColor": DOWN}}]
    overlay_cols, osc_cols = [], []
    for c in df.columns:
        base = c.split("(")[0]
        if base in ("ts", "time", "open", "high", "low", "close", "volume", "oi", "timeframe", "forming"):
            continue
        if c.endswith((".direction", ".bandwidth", ".percent_b", ".plus_di", ".minus_di")):
            continue
        (osc_cols if base in OSCILLATORS else overlay_cols).append(c)
    days = pd.to_datetime(df["ts"]).dt.date
    for i, c in enumerate(overlay_cols):
        band = ".upper" in c or ".lower" in c
        color = "#90A4AE" if band else LINE_COLORS[i % len(LINE_COLORS)]
        # VWAP restarts every session: draw one line per day so sessions are not joined by a jump.
        groups = [g for _, g in df.groupby(days)] if c.startswith("vwap") else [df]
        for gi, g in enumerate(groups):
            data = [{"time": t, "value": float(v)} for t, v in zip(g["time"], g[c]) if pd.notna(v)]
            if not data:
                continue
            last = gi == len(groups) - 1
            series.append({"type": "Line", "data": data, "options": {
                "color": color, "lineWidth": 1 if band else 2, "lineStyle": 2 if band else 0,
                "priceLineVisible": False, "lastValueVisible": False, "crosshairMarkerVisible": False,
                "title": (c.split("(")[0] + _short_params(c)) if last else ""}})
    charts = [{"chart": _base(height, title), "series": series}]

    if "volume" in df and df["volume"].fillna(0).sum() > 0:
        vol = [{"time": r.time, "value": float(r.volume or 0), "color": (UP if r.close >= r.open else DOWN) + "80"}
               for r in df.itertuples()]
        vchart = _base(110)
        vchart["timeScale"]["visible"] = False
        charts.append({"chart": vchart, "series": [{"type": "Histogram", "data": vol, "options": {
            "priceFormat": {"type": "volume"}, "priceLineVisible": False, "lastValueVisible": False}}]})
    for j, base in enumerate(sorted({c.split("(")[0] for c in osc_cols})):
        cols = [c for c in osc_cols if c.split("(")[0] == base]
        sers = []
        for i, c in enumerate(cols):
            data = [{"time": t, "value": float(v)} for t, v in zip(df["time"], df[c]) if pd.notna(v)]
            kind = "Histogram" if c.endswith(".hist") else "Line"
            sers.append({"type": kind, "data": data, "options": {
                "color": LINE_COLORS[i % len(LINE_COLORS)], "lineWidth": 2, "priceLineVisible": False,
                "lastValueVisible": True, "title": base + _short_params(c)}})
        ochart = _base(140)
        ochart["timeScale"]["visible"] = False
        charts.append({"chart": ochart, "series": sers})
    renderLightweightCharts(charts, key=key)


def _short_params(col: str) -> str:
    if "(" not in col:
        return ""
    inner = col[col.index("(") + 1:col.index(")")]
    vals = [p.split("=")[1] for p in inner.split(",") if "=" in p]
    suffix = col.split(")")[-1]
    return f" {','.join(vals)}{suffix}" if vals else suffix


def line_chart(points: list[tuple], *, title: str = "", height: int = 240, key: str = "line") -> None:
    """Equity-curve style area chart. `points`: (iso_ts, value)."""
    if not points:
        st.caption("No data yet.")
        return
    data, seen = [], set()
    for ts, v in points:
        t = _epoch(ts)
        if t in seen:
            continue
        seen.add(t)
        data.append({"time": t, "value": float(v)})
    data.sort(key=lambda d: d["time"])
    renderLightweightCharts([{"chart": _base(height, title), "series": [{"type": "Area", "data": data, "options": {
        "lineColor": "#2962FF", "topColor": "rgba(41,98,255,0.25)", "bottomColor": "rgba(41,98,255,0.02)",
        "lineWidth": 2}}]}], key=key)
