import streamlit as st

import api
import ui

PLAIN = {
    "ema_crossover": "Turns bullish when the fast moving average crosses above the slow one, bearish on the opposite cross.",
    "supertrend": "Follows the Supertrend line: bullish when price flips above it, bearish when it flips below.",
    "multi_confirmation": "Waits until trend (EMAs), momentum (RSI), VWAP, volume and trend strength (ADX) all agree.",
    "sr_breakout": "Trades breakouts of support/resistance, ranges and strong momentum candles.",
    "rsi_reversal": "Fades extremes: bullish when RSI climbs back out of oversold, bearish out of overbought.",
    "vwap_breakout": "Bullish when price crosses above VWAP on high volume, bearish below.",
}

snap = ui.snapshot()
if not ui.require_engine(snap):
    st.stop()
st.caption("Strategies watch the index/stock future and form a bullish or bearish view at each candle close. "
           "For **options** strategies the planner then picks *buy call / buy put / sell call / sell put*, the expiry "
           "and the strike. Every trade still has to pass the risk rules. Parameters live in `config/trading.yaml`.")


def _toggle(sid: str):
    on = st.session_state[f"strat_{sid}"]
    api.action(lambda: api.post(f"/engine/strategies/{sid}/{'enable' if on else 'disable'}"),
               f"{sid} {'enabled' if on else 'disabled'}")


for s in snap.get("strategies", []):
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        top.markdown(f"#### {s['id']}")
        if s.get("execution") == "options":
            top.badge("Options", icon=":material/candlestick_chart:", color="violet")
        else:
            top.badge("Futures", color="gray")
        top.toggle("Running", value=s["enabled"], key=f"strat_{s['id']}", on_change=_toggle, args=(s["id"],))
        und = ", ".join(i.split(":")[-1] for i in s.get("instruments_resolved") or s["instruments"])
        st.markdown(f"{PLAIN.get(s['type'], s['description'])}  \n"
                    f":gray[Watches **{und}** on **{s['timeframe']}** candles · type `{s['type']}`]")
        with st.container(horizontal=True):
            st.metric("Signals", s.get("signals", 0), border=True)
            st.metric("Trades", s.get("trades", 0), border=True)
            st.metric("Win rate", f"{s.get('win_rate_pct', 0):.0f}%", border=True)
            st.metric("P&L", ui.inr(s.get("net_pnl")), border=True)
            st.metric("Blocked by risk", s.get("risk_rejected", 0), border=True)
            st.metric("Skipped (weak)", s.get("ignored_low_score", 0), border=True)
        if s.get("status") not in ("enabled", "disabled"):
            st.warning(f"Status: {s['status']}")
        with st.expander("Parameters"):
            st.json(s["params"])
