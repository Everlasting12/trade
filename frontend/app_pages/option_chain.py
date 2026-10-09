import pandas as pd
import streamlit as st

import api
import ui

snap = ui.snapshot()
if not ui.require_engine(snap):
    st.stop()

unds = [u["name"] for u in (snap.get("underlyings") or []) if u.get("name")]
if not unds:
    st.info("The option chain is available for instruments your strategies analyse (see **Strategies**).")
    st.stop()

with st.container(horizontal=True, vertical_alignment="bottom"):
    und = st.segmented_control("Underlying", unds, default=unds[0], key="oc_und") or unds[0]
    width = st.select_slider("Strikes around ATM", [5, 8, 10, 12, 15, 20], value=10, key="oc_width")
    expiry_slot = st.empty()


@st.fragment(run_every="5s")
def chain_view(expiry: str | None):
    ch = api.safe_get("/engine/option-chain", underlying=und, expiry=expiry, width=width)
    if not ch:
        return
    q = pd.DataFrame(ch["quotes"])
    ce = q[q["type"] == "CE"].set_index("strike")
    pe = q[q["type"] == "PE"].set_index("strike")
    strikes = sorted(set(ce.index) | set(pe.index))
    df = pd.DataFrame({
        "CE OI": ce["oi"], "CE IV %": ce["iv"] * 100, "CE Δ": ce["delta"], "CE LTP": ce["ltp"],
        "Strike": strikes,
        "PE LTP": pe["ltp"], "PE Δ": pe["delta"], "PE IV %": pe["iv"] * 100, "PE OI": pe["oi"],
    }, index=strikes)
    ce_oi, pe_oi = (ce["oi"].fillna(0).sum(), pe["oi"].fillna(0).sum())
    with st.container(horizontal=True):
        st.metric(f"{und} forward", f"{ch['forward']:,.2f}", border=True,
                  help="Implied from put-call parity; used for IV/greeks")
        st.metric("ATM strike", f"{ch['atm_strike']:,.0f}" if ch.get("atm_strike") else "-", border=True)
        st.metric("ATM IV", f"{100 * ch['atm_iv']:.1f}%" if ch.get("atm_iv") else "-", border=True)
        st.metric("Put/Call OI ratio", f"{pe_oi / ce_oi:.2f}" if ce_oi else "-", border=True,
                  help=">1: more put open interest (often read as support)")
        st.metric("Days to expiry", ch["dte"], border=True)
    atm, fwd = ch.get("atm_strike"), ch["forward"]

    def style_row(row):
        k = row["Strike"]
        styles = []
        for col in row.index:
            if k == atm:
                styles.append("background-color: rgba(41,98,255,0.18); font-weight: 600")
            elif (col.startswith("CE") and k < fwd) or (col.startswith("PE") and k > fwd):
                styles.append("background-color: rgba(255,193,7,0.12)")   # in the money
            else:
                styles.append("")
        return styles

    styled = df.style.apply(style_row, axis=1).format({
        "CE OI": "{:,.0f}", "PE OI": "{:,.0f}", "CE IV %": "{:.1f}", "PE IV %": "{:.1f}", "CE Δ": "{:+.2f}",
        "PE Δ": "{:+.2f}", "CE LTP": "{:.2f}", "PE LTP": "{:.2f}", "Strike": "{:,.0f}"}, na_rep="-")
    st.dataframe(styled, hide_index=True, placeholder="-", height=min(38 * (len(df) + 1), 800), alt=f"{und} option chain")
    st.caption(f"Calls left, puts right. Shaded = in the money; highlighted = at the money. "
               f"Source: {'Kite live quotes' if ch['source'] == 'kite' else 'pricing model (simulated session)'}.")
    held = [p for p in (ui.snapshot().get("positions") or []) if p.get("underlying") == und]
    if held:
        st.markdown(f"**Your open {und} option trades**")
        for p in held:
            ui.position_card(p)


exps = (api.safe_get("/engine/option-chain", underlying=und, width=1) or {}).get("expiries", [])
expiry = expiry_slot.selectbox("Expiry", exps, key=f"oc_exp_{und}") if exps else None
chain_view(expiry)
