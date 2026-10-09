from datetime import date, timedelta

import pandas as pd
import streamlit as st

import api
import charts
import ui


@st.cache_data(ttl="10m")
def strategy_types():
    return api.get("/backtest/strategies")


try:
    types = strategy_types()
except api.ApiError as e:
    st.error(str(e))
    st.stop()

st.caption("Runs the same engine, strategies, options planner, risk rules and order handling as paper trading on "
           "past data. Orders fill on the next candle. **Option premiums in backtests are modelled** (Black-76 with "
           "a fixed IV), so treat option backtests as a check of the logic, and the paper pilot as the real test.")

by_type = {t["type"]: t for t in types}
with st.container(border=True):
    c1, c2, c3 = st.columns(3)
    stype = c1.selectbox("Strategy", list(by_type), format_func=lambda t: t.replace("_", " "))
    c1.caption(by_type[stype]["description"])
    execution = c2.segmented_control("Trade", ["Options", "Futures"], default="Options", key="bt_exec")
    tf = c3.segmented_control("Candle timeframe", ["1m", "3m", "5m", "15m", "30m", "1h", "1d"], default="5m",
                              key="bt_tf") or "5m"
    c1, c2, c3 = st.columns(3)
    source = c1.segmented_control("Data", ["synthetic", "kite", "csv"], default="synthetic", key="bt_source")
    style = c2.segmented_control("Options style", ["auto", "buy_only", "sell_only"], default="auto", key="bt_style",
                                 disabled=execution != "Options",
                                 help="auto: buy in trending markets, sell premium when ranging or options are expensive")
    iv = c3.slider("Model IV %", 8, 40, 14, disabled=execution != "Options",
                   help="Implied volatility used to price options in the backtest") / 100

with st.form("backtest"):
    st.markdown("**Strategy parameters**")
    params_df = st.data_editor(pd.DataFrame([{"param": k, "value": str(v)} for k, v in
                                             by_type[stype]["default_params"].items()]),
                               hide_index=True, disabled=["param"], key=f"params_{stype}")
    base_tf = "1d" if tf == "1d" else "1m"
    instrument, csv_rows, fdate, tdate = None, None, None, None
    synth_days, synth_seed, lot_size = 20, 42, 75
    if source == "kite":
        a, b, c = st.columns(3)
        instrument = a.text_input("Instrument", "FUT:NIFTY", help="e.g. FUT:NIFTY (near future), NSE:INFY")
        fdate = b.date_input("From", date.today() - timedelta(days=30))
        tdate = c.date_input("To", date.today())
        st.caption("Needs the Kite historical-data add-on. Expired contracts have no intraday history.")
    elif source == "csv":
        up = st.file_uploader("CSV with date, open, high, low, close[, volume, oi] (IST times)", type="csv")
        lot_size = st.number_input("Lot size", 1, 10000, 75)
        if up is not None:
            csv_rows = pd.read_csv(up).astype(str).to_dict("records")
    else:
        a, b, c = st.columns(3)
        synth_days = a.number_input("Trading days", 5, 250, 20)
        synth_seed = b.number_input("Random seed", 0, 10_000, 42)
        lot_size = c.number_input("Lot size", 1, 10000, 75)
        st.caption("Synthetic random-walk prices: checks the plumbing, says nothing about real performance.")

    st.markdown("**Money & risk**")
    a, b, c, d = st.columns(4)
    capital = a.number_input("Capital", 50_000, 100_000_000, 500_000, step=50_000)
    max_risk = b.number_input("Max loss per trade", 100, 1_000_000, 5000, step=500)
    max_loss = c.number_input("Max loss per day", 500, 5_000_000, 15000, step=1000)
    max_trades = d.number_input("Max trades per day", 1, 100, 5)
    a, b, c = st.columns(3)
    scoring_on = a.toggle("Signal scoring", True, help="Skip weak signals (score below the minimum)")
    min_score = b.slider("Min score", 0, 100, 60, disabled=not scoring_on)
    slippage = c.number_input("Slippage (bps)", 0.0, 50.0, 2.0)
    run = st.form_submit_button("Run backtest", type="primary", icon=":material/play_arrow:")

if run:
    params = {}
    for r in params_df.to_dict("records"):
        v = r["value"]
        try:
            params[r["param"]] = float(v) if "." in v else (v.lower() == "true" if v.lower() in ("true", "false") else int(v))
        except ValueError:
            params[r["param"]] = v
    body = {
        "strategy_type": stype, "params": params, "timeframe": tf, "base_timeframe": base_tf, "source": source,
        "instrument": instrument, "from_date": fdate.isoformat() if fdate else None,
        "to_date": tdate.isoformat() if tdate else None, "continuous": source == "kite" and tf == "1d",
        "csv_rows": csv_rows, "lot_size": int(lot_size), "synthetic_days": int(synth_days),
        "synthetic_seed": int(synth_seed), "capital": capital, "product": "MIS", "slippage_bps": slippage,
        "execution": "options" if execution == "Options" else "underlying", "options": {"style": style or "auto"},
        "option_iv": iv,
        "risk": {"max_risk_per_trade": max_risk, "max_daily_loss": max_loss, "max_trades_per_day": int(max_trades),
                 "allow_option_selling": True},
        "scoring": {"enabled": scoring_on, "min_score": min_score},
    }
    with st.spinner("Running backtest…"):
        st.session_state.bt_result = api.action(lambda: api.post("/backtest/run", body, timeout=600))

res = st.session_state.get("bt_result")
if res:
    m = res["metrics"]
    st.warning(res["disclaimer"], icon=":material/info:")
    with st.container(horizontal=True):
        st.metric("Net P&L", ui.inr(m["net_pnl"]), f"{m['total_return_pct']}%", border=True)
        st.metric("Trades", m["num_trades"], border=True)
        st.metric("Win rate", f"{m['win_rate_pct']}%", border=True)
        st.metric("Profit factor", m["profit_factor"] if m["profit_factor"] is not None else "-", border=True)
        st.metric("Max drawdown", ui.inr(m["max_drawdown"]), f"-{m['max_drawdown_pct']}%", border=True,
                  delta_color="inverse")
        st.metric("Sharpe", m["sharpe"] if m["sharpe"] is not None else "-", border=True)
        st.metric("Charges", ui.inr(m["total_charges"]), border=True)
    journal = res.get("journal") or []
    markers = []
    for j in journal:
        label = ui.ACTION_LABEL.get(j.get("action"), j.get("action") or "")
        if j.get("strike"):
            label += f" {j['strike']:g}"
        markers.append({"time": j["entry_time"], "kind": "entry", "label": label, "action": j.get("action")})
        if j.get("exit_time"):
            markers.append({"time": j["exit_time"], "kind": "exit", "action": j.get("action"),
                            "label": f"{(j.get('net_pnl') or 0):+,.0f}", "pnl": j.get("net_pnl")})
    tab_chart, tab_trades, tab_equity, tab_signals = st.tabs(["Chart", "Trades", "Equity curve", "Signals"])
    with tab_chart:
        charts.candle_chart(res.get("candles") or [], title=f"{res['timeframe']} backtest", markers=markers,
                            height=460, key="bt_chart")
    with tab_trades:
        if journal:
            st.dataframe(pd.DataFrame([{
                "entered": (j["entry_time"] or "")[:16].replace("T", " "),
                "trade": ui.ACTION_LABEL.get(j.get("action"), j.get("action")),
                "contract": j["instrument"].split(":")[-1], "qty": j["quantity"], "entry": j["entry_price"],
                "stop": j.get("stop_loss"), "target": j.get("target"), "plan R:R": j.get("planned_rr"),
                "exit": j.get("exit_price"), "exit reason": j.get("exit_reason"), "P&L": j.get("net_pnl"),
                "R": j.get("r_multiple"),
            } for j in journal]), hide_index=True, placeholder="-", alt="Backtest trades",
                column_config={"P&L": st.column_config.NumberColumn(format="₹%.0f")})
        else:
            st.caption("No trades.")
    with tab_equity:
        charts.line_chart(res["equity_curve"], title="Equity", key="bt_equity")
    with tab_signals:
        sig = pd.DataFrame(res["signals"])
        if not sig.empty:
            counts = sig["status"].map(lambda s: ui.STATUS_LABEL.get(s, s)).value_counts()
            st.bar_chart(counts, alt="Signal outcomes", height=200, horizontal=True)
            for x in res["signals"][:80]:
                st.markdown(ui.decision_line(x))
