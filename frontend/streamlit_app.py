"""Options trading dashboard. Run from the project root: uv run streamlit run frontend/streamlit_app.py

The dashboard is a pure client of the backend API: it never holds broker sessions or engine state,
so Streamlit reruns, multiple browser tabs or a UI crash cannot affect trading.
"""

import streamlit as st

import api
import ui

st.set_page_config(page_title="Options Autopilot", page_icon=":material/candlestick_chart:", layout="wide")

page = st.navigation({
    "Trade": [
        st.Page("app_pages/desk.py", title="Trading desk", icon=":material/space_dashboard:", default=True),
        st.Page("app_pages/watchlist.py", title="Watchlists", icon=":material/visibility:"),
        st.Page("app_pages/option_chain.py", title="Option chain", icon=":material/table_view:"),
        st.Page("app_pages/journal.py", title="Trade journal", icon=":material/menu_book:"),
    ],
    "Build & test": [
        st.Page("app_pages/strategies.py", title="Strategies", icon=":material/psychology:"),
        st.Page("app_pages/backtest.py", title="Backtest", icon=":material/history:"),
    ],
    "Settings": [
        st.Page("app_pages/risk.py", title="Risk & safety", icon=":material/shield:"),
        st.Page("app_pages/logs.py", title="Activity log", icon=":material/list_alt:"),
        st.Page("app_pages/account.py", title="Zerodha account", icon=":material/person:"),
    ],
})

with st.sidebar:
    try:
        auth = api.get("/auth/status")
        health = api.get("/engine/status")
    except api.ApiError as e:
        st.error(str(e))
        auth, health = None, None
    if auth:
        if auth["logged_in"]:
            p = auth.get("profile") or {}
            st.caption(f":material/verified_user: Kite: **{p.get('user_name', '')}** ({p.get('user_id', '')})")
            if st.button("Log out of Kite", icon=":material/logout:", width="stretch", type="tertiary"):
                api.action(lambda: api.post("/auth/logout"), "Logged out")
                st.rerun()
        elif auth["configured"]:
            st.link_button("Log in with Kite", auth["login_url"], type="primary", icon=":material/login:",
                           width="stretch")
            st.caption("Needed every morning (Kite tokens expire daily).")
        else:
            st.caption(":material/info: Kite not configured: set `BROKER_API_KEY` / `BROKER_API_SECRET` in `.env`.")
    st.divider()
    ui.sidebar_watchlist()
    if health and health["status"] != "running":
        st.caption(f"Engine: **{health['status']}**")
        if st.button("Start engine", icon=":material/play_arrow:", width="stretch"):
            res = api.action(lambda: api.post("/engine/start", timeout=120))
            if res and res.get("error"):
                st.error(res["error"])
            st.rerun()


@st.fragment(run_every="3s")
def header():
    ui.status_bar(ui.snapshot())


header()
page.run()
