import pandas as pd
import streamlit as st

import api
import ui

c1, c2 = st.columns([3, 1])
levels = c1.pills("Level", ["INFO", "WARNING", "ERROR", "CRITICAL"], selection_mode="multi",
                  default=["INFO", "WARNING", "ERROR", "CRITICAL"])
history = c2.toggle("From database (all history)")

if history:
    rows = api.safe_get("/engine/history/events", limit=2000) or []
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df[df["level"].isin(levels)]
        st.dataframe(df[["ts", "level", "event", "strategy", "instrument", "action", "reason", "order_id",
                         "position_id", "error", "data"]], hide_index=True, placeholder="-", alt="Event history", height=600)
else:
    @st.fragment(run_every="3s")
    def live_events():
        snap = ui.snapshot()
        rows = [r for r in snap.get("events", []) if r["level"] in levels]
        ui.show_table(rows, ["ts", "level", "event", "strategy", "instrument", "action", "reason", "order_id",
                             "position_id", "error", "data"], alt="Recent events", height=600)

    live_events()
st.caption("Every event is also written as JSON lines to `logs/events.jsonl` (rotated) for audit.")
