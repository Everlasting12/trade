"""Read-only view of the Zerodha account (independent of the engine)."""

import streamlit as st

import api
import ui

auth = api.safe_get("/auth/status")
if not auth or not auth["logged_in"]:
    st.info("Log in with Kite (sidebar) to see your Zerodha account.", icon=":material/login:")
    st.stop()

st.caption("Read-only. Orders are never placed from this page: all trading goes through the engine's risk "
           "manager and OMS.")
tabs = st.tabs(["Margins", "Holdings", "Positions", "Orders", "Trades"])

with tabs[0]:
    m = api.safe_get("/account/margins")
    if m:
        for seg in ("equity", "commodity"):
            d = m.get(seg)
            if not d or not d.get("enabled", True):
                continue
            st.subheader(seg.title())
            a, u = d.get("available", {}), d.get("utilised", {})
            with st.container(horizontal=True):
                st.metric("Net", ui.inr(d.get("net")), border=True)
                st.metric("Cash", ui.inr(a.get("cash")), border=True)
                st.metric("Live balance", ui.inr(a.get("live_balance")), border=True)
                st.metric("Used", ui.inr(u.get("debits")), border=True)
with tabs[1]:
    ui.show_table(api.safe_get("/account/holdings"), ["tradingsymbol", "exchange", "quantity", "average_price",
                                                      "last_price", "pnl", "day_change_percentage"], alt="Holdings")
with tabs[2]:
    p = api.safe_get("/account/positions") or {}
    ui.show_table(p.get("net"), ["tradingsymbol", "exchange", "product", "quantity", "average_price", "last_price",
                                 "pnl", "m2m"], alt="Broker positions")
with tabs[3]:
    ui.show_table(api.safe_get("/account/orders"), ["order_timestamp", "order_id", "tag", "tradingsymbol",
                                                    "transaction_type", "order_type", "product", "quantity",
                                                    "filled_quantity", "price", "trigger_price", "average_price",
                                                    "status", "status_message"], alt="Broker orders")
with tabs[4]:
    ui.show_table(api.safe_get("/account/trades"), ["fill_timestamp", "trade_id", "order_id", "tradingsymbol",
                                                    "transaction_type", "quantity", "average_price"],
                  alt="Broker trades")
