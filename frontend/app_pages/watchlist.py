"""Watchlists: multiple user lists with live prices, filters, sorting and a chart for the selected row."""

import pandas as pd
import streamlit as st

import api
import charts
import ui

TYPE_GROUPS = {"Stocks": {"EQ"}, "Indices": {"INDEX"}, "Futures": {"FUT"}, "Options": {"CE", "PE"}}
SORTS = {"List order": None, "Symbol": "symbol", "Change %": "change_pct", "LTP": "ltp", "Volume": "volume",
         "Open interest": "oi", "Day range position": "day_range_pos"}

lists = ui.watchlists()
if not lists:
    st.info("The backend isn't reachable yet. Start it, then come back.", icon=":material/cloud_off:")
    st.stop()
by_name = {w["name"]: w for w in lists}
st.caption("Your watchlists live in this app (Kite Connect doesn't expose the watchlists in your Kite app). "
           "Fill them by search, import your holdings/positions, or paste symbols copied from Kite.")

# ------------------------------------------------------------------ list selector + management
with st.container(horizontal=True, vertical_alignment="bottom"):
    default = st.session_state.get("wl_selected_list", lists[0]["name"])
    names = list(by_name)
    current = st.selectbox("Watchlist", names, index=names.index(default) if default in names else 0,
                           format_func=lambda n: f"{n}  ({by_name[n]['count']})", key="wl_current")
    st.session_state["wl_selected_list"] = current
    wid = by_name[current]["id"]
    with st.popover("New list", icon=":material/add:"):
        with st.form("new_list", clear_on_submit=True, border=False):
            new_name = st.text_input("Name", max_chars=40, placeholder="e.g. Banks, F&O stocks, Expiry day")
            if st.form_submit_button("Create", type="primary") and new_name.strip():
                if api.action(lambda: api.post("/watchlists", {"name": new_name}), f"Created {new_name}"):
                    st.session_state["wl_selected_list"] = new_name.strip()
                    st.rerun()
    with st.popover("List options", icon=":material/more_horiz:"):
        with st.form("rename", border=False):
            rn = st.text_input("Rename to", value=current, max_chars=40)
            if st.form_submit_button("Rename"):
                api.action(lambda: api.call("PATCH", f"/watchlists/{wid}", json={"name": rn}), "Renamed")
                st.session_state["wl_selected_list"] = rn
                st.rerun()
        st.divider()
        if st.checkbox(f"Delete '{current}' and its {by_name[current]['count']} instruments"):
            if st.button("Delete list", type="primary", icon=":material/delete:"):
                api.action(lambda: api.call("DELETE", f"/watchlists/{wid}"), f"Deleted {current}")
                st.session_state.pop("wl_selected_list", None)
                st.rerun()

# ------------------------------------------------------------------ add / import
with st.container(horizontal=True):
    with st.popover("Add instruments", icon=":material/playlist_add:", width="content"):
        q = st.text_input("Search", placeholder="INFY, RELIANCE, NIFTY, BANKNIFTY 26OCT, 25000CE…", key="wl_q")
        c1, c2 = st.columns(2)
        ex = c1.pills("Exchange", ["NSE", "BSE", "NFO", "BFO"], key="wl_ex")
        kind = c2.pills("Type", ["equity", "index", "future", "option"], key="wl_kind",
                        format_func=lambda k: {"equity": "Stock", "index": "Index", "future": "Future", "option": "Option"}[k])
        if q:
            params = {"q": q, "limit": 40}
            if ex:
                params["exchange"] = ex
            if kind:
                params["kind"] = kind
            res = api.safe_get("/watchlists/search", **params) or []
            if not res:
                st.caption("No matches.")
            else:
                pick = st.multiselect("Select", res, format_func=lambda r: f"{r['key']}  {r['name'] or ''} "
                                      f"{r.get('expiry') or ''} {r.get('strike') or ''}".strip(), key="wl_pick")
                if st.button(f"Add {len(pick)} to {current}", type="primary", disabled=not pick):
                    out = api.action(lambda: api.post(f"/watchlists/{wid}/items", {"keys": [p["key"] for p in pick]}))
                    if out:
                        st.toast(f"Added {out['added']}", icon=":material/check_circle:")
                        st.rerun()
    with st.popover("Import", icon=":material/download:", width="content"):
        st.markdown("**From Zerodha**")
        c1, c2 = st.columns(2)
        for col, src in ((c1, "holdings"), (c2, "positions")):
            if col.button(f"My {src}", key=f"imp_{src}", width="stretch"):
                out = api.action(lambda s=src: api.post(f"/watchlists/{wid}/import", {"source": s}))
                if out:
                    st.toast(f"Added {out['added']} from {src}", icon=":material/check_circle:")
                    st.rerun()
        st.markdown("**Paste symbols** (comma or one per line)")
        txt = st.text_area("Symbols", placeholder="INFY, TCS, HDFCBANK\nNIFTY 50\nBANKNIFTY\nNSE:SBIN",
                           label_visibility="collapsed", key="wl_paste")
        if st.button("Import symbols", disabled=not txt.strip()):
            out = api.action(lambda: api.post(f"/watchlists/{wid}/import", {"source": "symbols", "symbols": txt}))
            if out:
                msg = f"Added {out['added']}"
                if out["not_found"]:
                    msg += f"; not found: {', '.join(out['not_found'][:8])}"
                st.toast(msg, icon=":material/info:")
                st.rerun()

# ------------------------------------------------------------------ filters & sorting
with st.container(horizontal=True, vertical_alignment="bottom"):
    text = st.text_input("Filter", placeholder="Symbol or name", key="wl_filter", width=220)
    types = st.pills("Type", list(TYPE_GROUPS), selection_mode="multi", key="wl_types")
    move = st.segmented_control("Show", ["All", "Gainers", "Losers"], default="All", key="wl_move")
    sort_by = st.selectbox("Sort by", list(SORTS), key="wl_sort", width=170)
    desc = st.toggle("Descending", value=True, key="wl_desc")
    live = st.toggle("Live", value=True, key="wl_live", help="Refresh prices every 3 seconds")
chg_min, chg_max = st.slider("Change % range", -20.0, 20.0, (-20.0, 20.0), 0.5, key="wl_range")


@st.fragment(run_every="3s" if live else None)
def table():
    try:
        rows = api.get(f"/watchlists/{wid}/quotes")
    except api.ApiError as e:
        st.error(str(e))
        return
    if not rows:
        st.info(f"**{current}** is empty. Use **Add instruments** or **Import** above.", icon=":material/playlist_add:")
        return
    df = pd.DataFrame(rows)
    df = ui.numeric(df, ["ltp", "change", "change_pct", "open", "high", "low", "prev_close", "volume", "avg_price",
                         "oi", "bid", "ask", "upper_circuit", "lower_circuit", "day_range_pos", "strike"])
    if text:
        t = text.upper()
        df = df[df["symbol"].str.upper().str.contains(t, regex=False) | df["name"].fillna("").str.upper().str.contains(t, regex=False)]
    if types:
        allowed = set().union(*(TYPE_GROUPS[x] for x in types))
        df = df[df["type"].isin(allowed)]
    if move == "Gainers":
        df = df[df["change_pct"] > 0]
    elif move == "Losers":
        df = df[df["change_pct"] < 0]
    if (chg_min, chg_max) != (-20.0, 20.0):
        df = df[df["change_pct"].between(chg_min, chg_max)]
    if SORTS[sort_by]:
        df = df.sort_values(SORTS[sort_by], ascending=not desc, na_position="last")
    gainers, losers = int((df["change_pct"] > 0).sum()), int((df["change_pct"] < 0).sum())
    st.caption(f"{len(df)} instruments · :green[{gainers} up] · :red[{losers} down] · "
               f"{'live' if df['live'].any() else 'snapshot'} prices")
    cols = ["symbol", "ltp", "change", "change_pct", "open", "high", "low", "prev_close", "day_range_pos", "volume",
            "avg_price", "oi", "bid", "ask", "upper_circuit", "lower_circuit", "exchange", "type", "expiry",
            "lot_size", "updated"]
    # Hide columns that are empty for every row of this list (e.g. volume/OI for a list of indices).
    cols = [c for c in cols if c in ("symbol", "ltp") or df[c].notna().any()]
    view = df[cols].reset_index(drop=True)
    styled = view.style.map(ui.color_change, subset=[c for c in ("change", "change_pct") if c in view]).format({
        "ltp": "{:,.2f}", "change": "{:+,.2f}", "change_pct": "{:+.2f}%", "open": "{:,.2f}", "high": "{:,.2f}",
        "low": "{:,.2f}", "prev_close": "{:,.2f}", "avg_price": "{:,.2f}", "bid": "{:,.2f}", "ask": "{:,.2f}",
        "upper_circuit": "{:,.2f}", "lower_circuit": "{:,.2f}", "volume": "{:,.0f}", "oi": "{:,.0f}",
        "lot_size": "{:,.0f}"}, subset=pd.IndexSlice[:, [c for c in view.columns if c not in
                                                         ("symbol", "exchange", "type", "expiry", "updated", "day_range_pos")]],
        na_rep="-")
    ev = st.dataframe(styled, hide_index=True, placeholder="-", on_select="rerun", selection_mode="multi-row", key=f"wl_table_{wid}",
                      height=min(36 * (len(view) + 1) + 2, 640), alt=f"Watchlist {current}", column_config={
                          "symbol": st.column_config.TextColumn("Instrument", pinned=True),
                          "ltp": "LTP", "change": "Chg", "change_pct": "Chg %", "prev_close": "Prev close",
                          "open": "Open", "high": "High", "low": "Low", "volume": "Volume", "bid": "Bid",
                          "ask": "Ask", "exchange": "Exch", "type": "Type", "expiry": "Expiry",
                          "day_range_pos": st.column_config.ProgressColumn("Day range", min_value=0, max_value=1,
                                                                           format=" ",
                                                                           help="Where LTP sits between today's low and high"),
                          "avg_price": "Avg price", "oi": "OI", "upper_circuit": "Upper ckt",
                          "lower_circuit": "Lower ckt", "lot_size": "Lot", "updated": "Updated"})
    picked = [df.iloc[i]["key"] for i in (ev.selection.rows if ev else [])]
    if picked:
        st.session_state["wl_focus"] = picked[-1]
    with st.container(horizontal=True):
        if st.button(f"Remove {len(picked)} selected", icon=":material/delete:", disabled=not picked):
            api.action(lambda: api.post(f"/watchlists/{wid}/items/remove", {"keys": picked}), "Removed")
            st.rerun()
        if picked and len(lists) > 1:
            others = [n for n in by_name if n != current]
            target = st.selectbox("Copy to", others, key="wl_copy_to", label_visibility="collapsed", width=200)
            if st.button("Copy selected", icon=":material/content_copy:"):
                api.action(lambda: api.post(f"/watchlists/{by_name[target]['id']}/items", {"keys": picked}),
                           f"Copied to {target}")


table()

# ------------------------------------------------------------------ chart of the focused instrument
focus = st.session_state.get("wl_focus")
if focus:
    with st.container(border=True):
        top = st.container(horizontal=True, vertical_alignment="center")
        top.subheader(focus.split(":")[-1])
        tf = top.segmented_control("Timeframe", ["1m", "3m", "5m", "15m", "1h", "1d"], default="5m",
                                   key="wl_tf", label_visibility="collapsed") or "5m"
        days = {"1m": 2, "3m": 3, "5m": 5, "15m": 10, "1h": 30, "1d": 365}[tf]
        data = api.safe_get("/market/candles", instrument=focus, tf=tf, days=days, ind=["ema:9", "ema:21", "vwap"])
        if data and data.get("candles"):
            charts.candle_chart(data["candles"], title=f"{focus.split(':')[-1]} {tf}", markers=data.get("markers"),
                                height=420, key=f"wl_chart_{focus}_{tf}")
            pa = data.get("price_action") or {}
            st.caption(f"Structure **{pa.get('trend', '-')}** · data: {data['source']}"
                       + ("" if data["source"] == "kite" else
                          " (candles built live since this instrument was added; full history needs the Kite "
                          "historical-data add-on)"))
        elif data is not None:
            st.caption("No candles yet. Live candles build up from ticks after the instrument is added, during "
                       "market hours.")
else:
    st.caption("Select a row (or click an instrument in the sidebar watchlist) to see its chart.")
