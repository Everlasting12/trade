"""Trade journal: every trade with why/when/how it was entered and exited, plus pilot statistics."""

from datetime import date, timedelta

import pandas as pd
import streamlit as st

import api
import charts
import ui

with st.container(horizontal=True, vertical_alignment="bottom"):
    period = st.segmented_control("Period", ["Today", "7 days", "30 days", "All"], default="7 days", key="j_period")
    status = st.segmented_control("Status", ["All", "Open", "Closed"], default="All", key="j_status")

since = {"Today": date.today(), "7 days": date.today() - timedelta(days=7),
         "30 days": date.today() - timedelta(days=30)}.get(period)
params = {"limit": 2000}
if since:
    params["since"] = since.isoformat()
if status in ("Open", "Closed"):
    params["status"] = status.lower()
rows = api.safe_get("/engine/journal", **params) or []
stats = api.safe_get("/engine/journal/stats", **({"since": since.isoformat()} if since else {})) or {}

def group_table(groups: dict, first: str) -> None:
    df = pd.DataFrame([{first: k, "Trades": v["trades"], "P&L": v["net_pnl"], "Win %": v["win_rate_pct"],
                        "Avg R": v["avg_r"]} for k, v in groups.items()])
    df = ui.numeric(df, ["P&L", "Win %", "Avg R"])
    st.dataframe(df, hide_index=True, placeholder="-", alt=f"Results by {first.lower()}", column_config={
        "P&L": st.column_config.NumberColumn(format="₹%.0f"), "Win %": st.column_config.NumberColumn(format="%.0f%%"),
        "Avg R": st.column_config.NumberColumn(format="%.2f")})


o = stats.get("overall") or {}
with st.container(horizontal=True):
    st.metric("Closed trades", o.get("trades", 0), border=True)
    st.metric("Net P&L", ui.inr(o.get("net_pnl")), ui.signed(o.get("net_pnl")), border=True, help="After all charges")
    st.metric("Win rate", f"{o.get('win_rate_pct', 0):.0f}%", border=True)
    st.metric("Avg R", o.get("avg_r") if o.get("avg_r") is not None else "-", border=True,
              help="Average result in multiples of the planned risk (1R = the loss if the stop is hit)")
    st.metric("Profit factor", o.get("profit_factor") if o.get("profit_factor") is not None else "-", border=True,
              help="Gross wins / gross losses")
    st.metric("Avg win / loss", f"{ui.inr(o.get('avg_win'))} / {ui.inr(o.get('avg_loss'))}", border=True)

if stats.get("by_action"):
    c1, c2 = st.columns(2)
    with c1, st.container(border=True):
        st.markdown("**By trade type**")
        group_table({ui.ACTION_LABEL.get(k, k): v for k, v in stats["by_action"].items()}, "Trade type")
    with c2, st.container(border=True):
        st.markdown("**By exit reason**")
        group_table(stats["by_exit_reason"], "Exit")
    if len(stats.get("by_day") or {}) > 1:
        with st.container(border=True):
            st.markdown("**Daily P&L**")
            st.bar_chart(pd.DataFrame([{"day": d, "P&L": v["net_pnl"]} for d, v in stats["by_day"].items()]),
                         x="day", y="P&L", alt="Daily P&L", height=200)

if not rows:
    st.info("No trades in this period yet. Trades appear here as soon as the bot opens them.",
            icon=":material/menu_book:")
    st.stop()

df = pd.DataFrame([{
    "entered": (r["entry_time"] or "")[:16].replace("T", " "), "trade": ui.ACTION_LABEL.get(r.get("action"), r.get("action")),
    "contract": r["instrument"].split(":")[-1], "lots": r.get("lots"), "entry": r["entry_price"],
    "stop": r.get("stop_loss"), "target": r.get("target"), "plan R:R": r.get("planned_rr"),
    "exited": (r.get("exit_time") or "")[11:16], "exit": r.get("exit_price"), "exit reason": r.get("exit_reason") or "open",
    "P&L": r.get("net_pnl"), "R": r.get("r_multiple"), "strategy": r["strategy"], "status": r["status"],
} for r in rows])
df = ui.numeric(df, ["entry", "stop", "target", "plan R:R", "exit", "P&L", "R"])
st.markdown("**Trades** · select a row for the full story")
sel = st.dataframe(df, hide_index=True, placeholder="-", alt="Trade journal", on_select="rerun", selection_mode="single-row",
                   key="journal_table", column_config={
                       "P&L": st.column_config.NumberColumn(format="₹%.0f"),
                       "R": st.column_config.NumberColumn(format="%.2f")})
picked = sel.selection.rows if sel else []
if picked:
    r = rows[picked[0]]
    with st.container(border=True):
        st.subheader(f"{ui.ACTION_LABEL.get(r.get('action'), r.get('action'))} · {r['instrument'].split(':')[-1]}")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown("**Entry**")
            st.markdown(f"{(r['entry_time'] or '')[:19].replace('T', ' ')}  \n"
                        f"Premium **{r['entry_price']}** × {r['quantity']} ({r.get('lots')} lot)  \n"
                        f"Stop **{r.get('stop_loss')}** · target **{r.get('target')}** · planned R:R **{r.get('planned_rr')}**  \n"
                        f"Planned risk **{ui.inr(r.get('planned_risk'))}**")
        with c2:
            st.markdown("**Underlying**")
            st.markdown(f"{r.get('underlying')} ({r.get('direction')})  \n"
                        f"at entry **{r.get('underlying_entry')}** · exit **{r.get('underlying_exit') or '-'}**  \n"
                        f"stop **{r.get('underlying_sl')}** · target **{r.get('underlying_target')}**  \n"
                        f"strike {r.get('strike')} · expiry {r.get('expiry')} · {r.get('dte')} days")
        with c3:
            st.markdown("**Exit & result**")
            st.markdown(f"{(r.get('exit_time') or 'still open')[:19].replace('T', ' ')}  \n"
                        f"Premium **{r.get('exit_price') or '-'}** · {r.get('exit_reason') or ''}  \n"
                        f"Net {ui.pnl_text(r.get('net_pnl'))} · R **{r.get('r_multiple')}** · charges {ui.inr(r.get('charges'))}  \n"
                        f"Held {ui.duration(r.get('duration_s'))} · best {ui.inr(r.get('mfe'))} / worst {ui.inr(r.get('mae'))}")
        st.markdown("**Why the bot took it**")
        for x in (r.get("reasons") or []):
            st.markdown(f"- {x}")
        st.caption(f"Market at entry: IV {r.get('iv') and round(100 * r['iv'], 1)}% · ATM IV "
                   f"{r.get('atm_iv') and round(100 * r['atm_iv'], 1)}% · realised vol "
                   f"{r.get('realized_vol') and round(100 * r['realized_vol'], 1)}% · ADX {r.get('adx') and round(r['adx'], 1)} · "
                   f"delta {r.get('delta') and round(r['delta'], 2)} · signal score {r.get('score')}")
        if r.get("underlying"):
            key = next((u["key"] for u in (ui.snapshot().get("underlyings") or []) if u.get("name") == r["underlying"]), None)
            if key:
                data = api.safe_get("/market/candles", instrument=key, tf=r.get("timeframe") or "5m", days=3)
                if data:
                    mk = [m for m in data.get("markers", []) if m["time"][:16] in
                          ((r["entry_time"] or "")[:16], (r.get("exit_time") or "")[:16])]
                    charts.candle_chart(data["candles"], title=f"{r['underlying']} around this trade", markers=mk,
                                        height=320, key=f"j_{r['position_id']}")

st.download_button("Download journal (CSV)", df.to_csv(index=False), "trade_journal.csv", "text/csv",
                   icon=":material/download:")
