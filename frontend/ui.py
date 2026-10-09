"""Shared UI helpers: engine snapshot, status bar, plain-language labels, position cards, decision feed."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import api

ACTION_LABEL = {"BUY_CE": "Buy call", "BUY_PE": "Buy put", "SELL_CE": "Sell call", "SELL_PE": "Sell put",
                "BUY": "Buy", "SELL": "Sell", "EXIT": "Exit"}
ACTION_ICON = {"BUY_CE": ":material/trending_up:", "SELL_PE": ":material/trending_up:",
               "BUY_PE": ":material/trending_down:", "SELL_CE": ":material/trending_down:"}
STATUS_LABEL = {
    "approved": "Order placed", "risk_rejected": "Blocked by risk rules", "ignored_low_score": "Skipped (weak signal)",
    "paused": "Not traded (auto-trading off)", "no_contract": "No suitable option contract",
    "already_in_position": "Skipped (already in a trade)", "no_position": "Exit signal (no open trade)",
}


def snapshot() -> dict:
    try:
        return api.get("/engine/snapshot")
    except api.ApiError as e:
        return {"status": "unreachable", "error": str(e)}


def inr(x, decimals: int = 0) -> str:
    if x is None:
        return "-"
    sign = "-" if x < 0 else ""
    return f"{sign}₹{abs(x):,.{decimals}f}"


def signed(x) -> int | str:
    """Metric delta: 0 renders neutral (grey, no arrow); otherwise a signed rupee string."""
    if not x:
        return 0
    return f"{'+' if x > 0 else '-'}₹{abs(x):,.0f}"


def numeric(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    return df


def pnl_text(x, decimals: int = 0) -> str:
    if x is None:
        return "-"
    color = "green" if x > 0 else "red" if x < 0 else "gray"
    return f":{color}[{'+' if x > 0 else ''}{inr(x, decimals)}]"


def hhmm(ts: str | None) -> str:
    return (ts or "")[11:16]


def duration(seconds) -> str:
    if seconds is None:
        return "-"
    return str(timedelta(seconds=int(seconds)))


def require_engine(snap: dict) -> bool:
    status = snap.get("status")
    if status == "running":
        return True
    if status == "unreachable":
        st.error(snap.get("error"), icon=":material/cloud_off:")
    elif status == "waiting_for_login":
        st.info("**Log in with Kite** (sidebar) to start live paper trading, or start the backend with "
                "`FEED_SOURCE=synthetic` to try a simulated market.", icon=":material/login:")
    else:
        st.warning(f"Engine is **{status}**. {snap.get('error') or ''}", icon=":material/error:")
    return False


def show_table(rows, columns: list[str] | None = None, alt: str = "table", **kw) -> None:
    df = pd.DataFrame(rows or [])
    if df.empty:
        st.caption("Nothing here yet.")
        return
    if columns:
        df = df[[c for c in columns if c in df.columns]]
    st.dataframe(df, hide_index=True, placeholder="-", alt=alt, **kw)


# ------------------------------------------------------------------ status bar

def _toggle_auto():
    key = st.session_state["_auto_key"]
    on = st.session_state[key]
    api.action(lambda: api.post("/engine/auto-trading", {"on": on}),
               "Auto-trading ON: new trades will be taken" if on else "Auto-trading OFF: no new trades")


@st.dialog("Emergency stop")
def kill_dialog():
    st.write("This **closes every open position at market**, cancels all pending orders and blocks new trades "
             "until you reset it on *Risk & safety*.")
    reason = st.text_input("Reason", "manual emergency stop")
    if st.button("Stop everything now", type="primary", icon=":material/block:", width="stretch"):
        api.action(lambda: api.post("/engine/kill-switch", {"reason": reason, "flatten": True}), "Kill switch activated")
        st.rerun()


def status_bar(snap: dict) -> None:
    """Mode, engine/feed state, auto-trading switch and emergency stop. Live mode is unmistakable."""
    mode = (snap.get("mode") or "paper").lower()
    running = snap.get("status") == "running"
    ov = snap.get("overview") or {}
    ks = ov.get("kill_switch") or {}
    if mode == "live":
        guard = snap.get("live_guard") or {}
        st.error(f"### 🔴 LIVE TRADING: real orders go to Zerodha  \n"
                 f"{'**ARMED** by ' + guard.get('armed_by', '') if guard.get('armed') else 'Not armed: ' + str(guard.get('block_reason'))}",
                 icon=":material/warning:")
    with st.container(border=True, horizontal=True, vertical_alignment="center", gap="medium"):
        if mode == "live":
            st.badge("LIVE TRADING", icon=":material/bolt:", color="red")
        else:
            st.badge("PAPER TRADING", icon=":material/science:", color="green",
                     help="Simulated orders on live market data. Nothing is sent to Zerodha.")
        feed = snap.get("feed") or {}
        if running:
            stale = any(m.get("stale") for m in (snap.get("market") or {}).values())
            st.badge(f"Market data: {'delayed' if stale else 'live'}" + (" (simulated)" if feed.get("source") == "synthetic" else ""),
                     icon=":material/sensors:", color="orange" if stale else "blue")
            st.caption(f"Engine clock {hhmm(snap.get('clock'))}")
        else:
            st.badge(f"Engine {snap.get('status', 'offline')}", icon=":material/power_off:", color="gray")
        if running:
            on = bool(snap.get("auto_trading", True))
            key = f"auto_toggle_{on}"
            st.session_state["_auto_key"] = key
            st.toggle("**Auto-trading**", value=on, key=key, on_change=_toggle_auto,
                      help="ON: the bot opens new option trades by itself. OFF: no new trades; open trades are "
                           "still managed (stops, targets, square-off).")
            if ks.get("active"):
                st.badge("KILL SWITCH ACTIVE", icon=":material/block:", color="red", help=ks.get("reason"))
            elif st.button("Emergency stop", icon=":material/emergency_home:", type="secondary"):
                kill_dialog()
    if ks.get("active"):
        st.error(f"**Kill switch active**: {ks.get('reason')}. New trades are blocked until reset on *Risk & safety*.",
                 icon=":material/block:")
    if ov.get("daily_halt"):
        st.warning(f"**Trading halted for today**: {ov['daily_halt']}", icon=":material/pause_circle:")


# ------------------------------------------------------------------ positions & decisions

def contract_label(p: dict) -> str:
    inst = (p.get("instrument") or "").split(":")[-1]
    return inst


@st.dialog("Exit position")
def exit_dialog(instrument: str):
    st.write(f"Close **{instrument}** at market now? Its stop-loss and target orders will be cancelled.")
    if st.button("Exit now", type="primary", icon=":material/logout:", width="stretch"):
        api.action(lambda: api.post("/engine/positions/exit", {"instrument": instrument, "reason": "manual exit (UI)"}),
                   f"Exit sent for {instrument}")
        st.rerun()


def position_card(p: dict) -> None:
    action = p.get("option_action")
    label = ACTION_LABEL.get(action, p.get("side", "").title())
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        top.markdown(f"{ACTION_ICON.get(action, ':material/swap_vert:')} **{label}** · `{contract_label(p)}`")
        if top.button("Exit", key=f"exit_{p['instrument']}", icon=":material/logout:", type="tertiary"):
            exit_dialog(p["instrument"])
        pnl = p.get("unrealized_pnl") or 0
        st.markdown(f"### {pnl_text(pnl)}  :gray[({p.get('pnl_pct', 0):+.1f}%)]")
        c1, c2, c3 = st.columns(3)
        c1.caption(f"Entry **{p['avg_price']:.2f}**")
        c2.caption(f"Now **{p.get('last_price', 0):.2f}**")
        c3.caption(f"Qty **{abs(p['quantity'])}** ({p.get('lots', '-')} lot)")
        c1, c2, c3 = st.columns(3)
        c1.caption(f"Stop :red[{p['stop_loss']:.2f}]" if p.get("stop_loss") else "Stop -")
        c2.caption(f"Target :green[{p['target']:.2f}]" if p.get("target") else "Target -")
        c3.caption(f"R:R **{p['planned_rr']}**" if p.get("planned_rr") else "")
        if p.get("underlying"):
            st.caption(f"{p['underlying']} now **{(p.get('underlying_last') or 0):,.2f}** · exit if it crosses "
                       f"stop **{p.get('underlying_sl')}** / target **{p.get('underlying_target')}** · "
                       f"{p.get('strategy')} · open {duration(p.get('duration_s'))}")


def decision_line(s: dict) -> str:
    meta = s.get("metadata") or {}
    t = hhmm(s.get("ts") if isinstance(s.get("ts"), str) else str(s.get("ts")))
    status = s.get("status")
    action = meta.get("option_action")
    inst = (s.get("instrument") or "").split(":")[-1]
    if status == "approved" and action:
        return (f"**{t}** :green[{ACTION_LABEL[action]}] `{inst}` × {s.get('quantity')} @ {s.get('entry_price')} "
                f"· stop {s.get('stop_loss')} · target {s.get('target')} · R:R {meta.get('planned_rr')}")
    if str(s.get("action")) == "EXIT":
        why = "; ".join(s.get("reason") or []) or "strategy exit"
        if status == "approved":
            return f"**{t}** :orange[Exit] trade on `{inst}`: {why}"
        return f"**{t}** :gray[Exit signal on `{inst}` (no open trade)]: {why}"
    if status == "approved":
        return f"**{t}** :green[{s.get('action')}] `{inst}` × {s.get('quantity')}"
    reasons = "; ".join(((s.get("risk") or {}).get("reasons") or [])[:2])
    view = {"BUY": "bullish", "SELL": "bearish", "EXIT": "exit"}.get(str(s.get("action")), str(s.get("action")))
    what = f"{ACTION_LABEL.get(action, view)} `{inst}`" if action else f"{view} signal on `{inst}`"
    extra = f" (score {s.get('score')})" if status == "ignored_low_score" else (f": {reasons}" if reasons else "")
    return f"**{t}** :gray[{STATUS_LABEL.get(status, status)}]: {what}{extra}"


# ------------------------------------------------------------------ watchlists

def color_change(v):
    if v is None or pd.isna(v) or v == 0:
        return ""
    return "color: #26a69a" if v > 0 else "color: #ef5350"


def watchlists() -> list[dict]:
    try:
        return api.get("/watchlists")
    except api.ApiError:
        return []


@st.fragment(run_every="3s")
def sidebar_watchlist():
    """Compact Kite-style watchlist in the sidebar. Clicking a row opens it on the Watchlists page."""
    lists = watchlists()
    if not lists:
        st.caption("Watchlists appear here once the backend is running.")
        return
    names = {w["name"]: w["id"] for w in lists}
    top = st.container(horizontal=True, vertical_alignment="center")
    name = top.selectbox("Watchlist", list(names), key="sb_wl", label_visibility="collapsed")
    top.page_link("app_pages/watchlist.py", label="Manage", icon=":material/open_in_new:")
    wid = names[name]
    try:
        rows = api.get(f"/watchlists/{wid}/quotes")
    except api.ApiError as e:
        st.caption(str(e))
        return
    if not rows:
        st.caption("Empty list. Add instruments on the **Watchlists** page.")
        return
    df = pd.DataFrame([{"Symbol": r["symbol"], "LTP": r["ltp"], "Chg %": r["change_pct"]} for r in rows])
    df = numeric(df, ["LTP", "Chg %"])
    styled = df.style.map(color_change, subset=["Chg %"]).format({"LTP": "{:,.1f}", "Chg %": "{:+.1f}%"}, na_rep="-")
    nonce = st.session_state.setdefault("sb_nonce", 0)
    ev = st.dataframe(styled, hide_index=True, placeholder="-", on_select="rerun", selection_mode="single-row",
                      key=f"sb_tbl_{nonce}", height=min(36 * (len(df) + 1) + 2, 420), alt="Watchlist",
                      column_config={"Symbol": st.column_config.TextColumn(width=88),
                                     "LTP": st.column_config.TextColumn(width=66),
                                     "Chg %": st.column_config.TextColumn(width=54)})
    if ev and ev.selection.rows:
        st.session_state["wl_focus"] = rows[ev.selection.rows[0]]["key"]
        st.session_state["wl_selected_list"] = name
        st.session_state["sb_nonce"] = nonce + 1
        st.switch_page("app_pages/watchlist.py")
