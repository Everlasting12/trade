import streamlit as st

import api
import ui

snap = ui.snapshot()
if not ui.require_engine(snap):
    st.stop()
ov = snap["overview"]
rc = dict(snap.get("risk_config") or {})

c1, c2 = st.columns(2)
with c1, st.container(border=True):
    st.subheader(":material/emergency_home: Emergency stop")
    ks = ov["kill_switch"]
    if ks["active"]:
        st.error(f"**Active** since {ks['ts'][:19]}: {ks['reason']}")
        with st.form("reset_ks"):
            ok = st.checkbox("I've checked positions and orders; allow new trades again")
            if st.form_submit_button("Reset emergency stop", icon=":material/restart_alt:"):
                if ok:
                    api.action(lambda: api.post("/engine/kill-switch/reset", {"confirm": True}), "Reset")
                    st.rerun()
                else:
                    st.error("Tick the confirmation first.")
    else:
        st.caption("Closes every position at market, cancels pending orders and blocks new trades until reset. "
                   "Also trips automatically if the account drawdown exceeds the limit below.")
        if st.button("Stop everything now", type="primary", icon=":material/block:"):
            ui.kill_dialog()

with c2, st.container(border=True):
    st.subheader(":material/smart_toy: Auto-trading")
    on = snap.get("auto_trading", True)
    st.markdown(f"Currently **{'ON' if on else 'OFF'}**. "
                + ("The bot opens new trades on its own." if on else "No new trades; open trades are still managed."))
    if st.button("Turn OFF" if on else "Turn ON", icon=":material/pause:" if on else ":material/play_arrow:"):
        api.action(lambda: api.post("/engine/auto-trading", {"on": not on}))
        st.rerun()

with st.container(border=True):
    st.subheader(":material/rule: Your risk rules")
    st.caption("Every trade the bot wants to take must pass all of these. Edit them in `config/trading.yaml` (or "
               "`MAX_*` in `.env`) and restart the backend.")
    rules = [
        ("Max loss on one trade", ui.inr(rc.get("max_risk_per_trade")), "Position size is calculated so a stop-out loses at most this"),
        ("Max loss in a day", ui.inr(rc.get("max_daily_loss")), "Trading stops for the day (and positions close) when reached"),
        ("Max trades per day", rc.get("max_trades_per_day"), ""),
        ("Max open trades", rc.get("max_open_positions"), ""),
        ("Stop after consecutive losses", rc.get("max_consecutive_losses"), "Trading stops for the day"),
        ("Account drawdown kill switch", f"{rc.get('max_drawdown_pct')}%", "Emergency stop trips automatically"),
        ("New trades allowed", f"{rc.get('no_entries_before')} – {rc.get('no_new_entries_after')}", "Avoids the open and the last half hour"),
        ("Intraday square-off", rc.get("square_off_time"), "All intraday (MIS) positions are closed"),
        ("Option selling", "allowed" if rc.get("allow_option_selling") else "not allowed",
         f"Max {rc.get('max_short_option_lots')} lot per sell; loss can exceed the premium received"),
        ("Max premium per option buy", ui.inr(rc.get("max_premium_per_trade")), ""),
        ("Minimum option premium", rc.get("min_option_premium"), "Avoids illiquid far-OTM contracts"),
        ("Stale market data", f"{rc.get('stale_data_seconds')} s", "No new trades if prices stop updating"),
    ]
    for name, value, note in rules:
        c1, c2, c3 = st.columns([2, 1, 3])
        c1.markdown(name)
        c2.markdown(f"**{value}**")
        c3.caption(note)

with st.container(border=True):
    st.subheader(":material/bolt: Live trading on Zerodha")
    guard = snap.get("live_guard")
    if not guard:
        st.info("You are in **paper trading**: nothing is sent to Zerodha. After a successful pilot, live trading "
                "needs: `TRADING_MODE=live` and `LIVE_TRADING_ENABLED=true` in `.env` (then restart), a clean "
                "position check with Zerodha, and arming here with a typed confirmation (expires daily). Start with "
                "`LIVE_MAX_ORDER_LOTS=1`.", icon=":material/lock:")
    elif guard["armed"]:
        st.error(f"ARMED by {guard['armed_by']} at {guard['armed_at'][:19]}: the bot can send REAL orders.")
        if st.button("Disarm live trading", type="primary", icon=":material/lock:"):
            api.action(lambda: api.post("/engine/live/disarm"), "Disarmed")
            st.rerun()
    else:
        st.warning(f"Not armed: {guard['block_reason']}")
        if guard["reconcile_issues"]:
            st.error("Zerodha and the app disagree:\n\n- " + "\n- ".join(guard["reconcile_issues"]))
            if st.button("I resolved these manually"):
                api.action(lambda: api.post("/engine/live/acknowledge-reconciliation", {"operator": "ui"}))
                st.rerun()
        with st.form("arm"):
            st.markdown(f"Type exactly: `{guard['confirm_phrase']}`")
            phrase = st.text_input("Confirmation phrase")
            operator = st.text_input("Your name", max_chars=60)
            if st.form_submit_button("Arm live trading", icon=":material/bolt:"):
                api.action(lambda: api.post("/engine/live/arm", {"phrase": phrase, "operator": operator or "operator"}),
                           "Live trading armed")
                st.rerun()
