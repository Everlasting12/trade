import pandas as pd
import streamlit as st

import api
import charts
import ui

TIMEFRAMES = {"1m": 2, "3m": 3, "5m": 5, "15m": 10, "1h": 30, "1d": 365}   # tf -> days of history
INDICATORS = {"EMA 9": "ema:9", "EMA 21": "ema:21", "EMA 50": "ema:50", "VWAP": "vwap",
              "Supertrend": "supertrend:10,2", "Bollinger": "bbands:20,2", "RSI": "rsi:14", "MACD": "macd:12,26,9",
              "ADX": "adx:14"}

snap = ui.snapshot()
if not ui.require_engine(snap):
    st.stop()
today = (snap.get("clock") or "")[:10]


@st.fragment(run_every="3s")
def kpis():
    s = ui.snapshot()
    ov = s.get("overview") or {}
    if not ov:
        return
    open_pnl = ov.get("unrealized_pnl") or 0
    with st.container(horizontal=True):
        st.metric("Today's P&L", ui.inr(ov["todays_pnl"]), ui.signed(ov["todays_pnl"]), border=True,
                  help="Closed trades today (after charges) + open trades")
        st.metric("Open P&L", ui.inr(open_pnl), ui.signed(open_pnl), border=True)
        st.metric("Trades today", ov["entries_today"], border=True,
                  help=f"Max {(s.get('risk_config') or {}).get('max_trades_per_day', '-')} per day")
        st.metric("Win rate", f"{ov['win_rate_pct']:.0f}%", border=True, help=f"{ov['total_trades']} closed trades")
        st.metric("Capital", ui.inr(ov["equity"]), ui.signed(ov["equity"] - ov["capital"]), border=True,
                  help=f"Started with {ui.inr(ov['capital'])} · free margin {ui.inr(ov['available_margin'])}")


kpis()

left, right = st.columns([2.3, 1], gap="medium")

with left:
    unds = snap.get("underlyings") or []
    if not unds:
        st.info("No strategies are running on any instrument. Enable one on **Strategies**.", icon=":material/info:")
    else:
        with st.container(horizontal=True, vertical_alignment="bottom"):
            names = [u["name"] or u["key"] for u in unds]
            pick = st.segmented_control("Instrument", names, default=names[0], key="desk_und")
            tf = st.segmented_control("Timeframe", list(TIMEFRAMES), default="5m", key="desk_tf")
            with st.popover("Indicators", icon=":material/stacked_line_chart:"):
                chosen = st.pills("Show", list(INDICATORS), selection_mode="multi",
                                  default=["EMA 9", "EMA 21", "VWAP", "RSI"], key="desk_ind")
            live = st.toggle("Live update", value=False, key="desk_live",
                             help="Redraw every 15 s. Turn off to zoom/scroll freely (redraw resets the view).")
        und = next((u for u in unds if (u["name"] or u["key"]) == pick), unds[0])
        tf = tf or "5m"
        # Any instrument from the sidebar's watchlist can be charted here too.
        wl = next((w for w in ui.watchlists() if w["name"] == st.session_state.get("sb_wl")), None)
        wl_items = api.safe_get(f"/watchlists/{wl['id']}/items") if wl else []
        other = st.selectbox("Or chart from your watchlist", ["-"] + [i["instrument_key"] for i in wl_items or []],
                             key="desk_other", format_func=lambda k: k if k == "-" else k.split(":")[-1])
        if other != "-":
            und = {"key": other, "name": other.split(":")[-1], "token": None}

        @st.fragment(run_every="15s" if live else None)
        def price_chart():
            data = api.safe_get("/market/candles", instrument=und["key"], tf=tf, days=TIMEFRAMES[tf],
                                ind=[INDICATORS[i] for i in (chosen or [])])
            if not data:
                return
            charts.candle_chart(data["candles"], title=f"{und['name']} {tf}", markers=data.get("markers"),
                                key=f"desk_{und['key']}_{tf}")
            pa = data.get("price_action") or {}
            pats = [p for p in data.get("candlestick_patterns", []) if p["direction"] != "neutral"][-3:]
            bits = [f"Structure **{pa.get('trend', '-')}**"]
            if pa.get("nearest_support"):
                bits.append(f"support **{pa['nearest_support']:,.0f}**")
            if pa.get("nearest_resistance"):
                bits.append(f"resistance **{pa['nearest_resistance']:,.0f}**")
            if pats:
                bits.append("patterns: " + ", ".join(f"{p['pattern'].replace('_', ' ')} ({p['direction']})" for p in pats))
            st.caption(" · ".join(bits) + f" · data: {data['source']}")

        price_chart()

with right:
    @st.fragment(run_every="2s")
    def live_panel():
        s = ui.snapshot()
        positions = s.get("positions") or []
        st.markdown(f"**Open positions** ({len(positions)})")
        if not positions:
            st.caption("No open trades. When a strategy signals and the risk checks pass, the bot opens an option "
                       "trade here." + ("" if s.get("auto_trading") else " **Auto-trading is OFF.**"))
        for p in positions:
            ui.position_card(p)
        st.markdown("**What the bot decided**")
        sigs = (s.get("signals") or [])[:8]
        if not sigs:
            st.caption("No signals yet today. Strategies decide at each candle close.")
        with st.container(height=330 if sigs else "content", border=False):
            for x in sigs:
                st.markdown(ui.decision_line(x))

    live_panel()

tab_trades, tab_orders, tab_decisions = st.tabs(["Today's trades", "Orders", "All decisions"])
with tab_trades:
    rows = api.safe_get("/engine/journal", since=today) or []
    if rows:
        df = pd.DataFrame([{
            "time": ui.hhmm(r["entry_time"]), "trade": ui.ACTION_LABEL.get(r.get("action"), r.get("action")),
            "contract": r["instrument"].split(":")[-1], "qty": r["quantity"], "entry": r["entry_price"],
            "exit": r.get("exit_price"), "P&L": r.get("net_pnl"), "R": r.get("r_multiple"),
            "exit reason": r.get("exit_reason") or "open", "strategy": r["strategy"],
        } for r in rows])
        df = ui.numeric(df, ["entry", "exit", "P&L", "R"])
        st.dataframe(df, hide_index=True, placeholder="-", alt="Today's trades", column_config={
            "P&L": st.column_config.NumberColumn(format="₹%.0f"), "R": st.column_config.NumberColumn(format="%.2f")})
    else:
        st.caption("No trades today.")
with tab_orders:
    ui.show_table(snap.get("orders"), ["created_at", "instrument", "role", "side", "order_type", "quantity",
                                       "filled_qty", "price", "trigger_price", "avg_fill_price", "status",
                                       "reject_reason"], alt="Orders")
with tab_decisions:
    for x in (snap.get("signals") or [])[:60]:
        st.markdown(ui.decision_line(x))
