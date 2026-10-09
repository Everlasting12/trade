"""Timeframe definitions and session-aligned candle bucketing.

Intraday buckets are anchored at the 09:15 session open (this is how Kite/NSE align 3m, 15m,
30m and 1h candles), and the last bucket of the day is clipped at the 15:30 close.
"""

from datetime import datetime, timedelta

from app.core.clock import IST, SESSION_CLOSE, SESSION_OPEN, to_ist

TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "10m": 600, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400,
}
KITE_INTERVAL: dict[str, str] = {
    "1m": "minute", "3m": "3minute", "5m": "5minute", "10m": "10minute", "15m": "15minute",
    "30m": "30minute", "1h": "60minute", "1d": "day",
}
# Max days per Kite historical request, per interval.
KITE_MAX_DAYS: dict[str, int] = {
    "1m": 60, "3m": 100, "5m": 100, "10m": 100, "15m": 200, "30m": 200, "1h": 400, "1d": 2000,
}
TIMEFRAMES = list(TIMEFRAME_SECONDS)


def validate(tf: str) -> str:
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(f"Unknown timeframe {tf!r}; use one of {TIMEFRAMES}")
    return tf


def seconds(tf: str) -> int:
    return TIMEFRAME_SECONDS[validate(tf)]


def bucket_start(ts: datetime, tf: str) -> datetime:
    ts = to_ist(ts)
    if tf == "1d":
        return datetime(ts.year, ts.month, ts.day, tzinfo=IST)
    secs = seconds(tf)
    open_dt = datetime.combine(ts.date(), SESSION_OPEN, IST)
    offset = (ts - open_dt).total_seconds()
    if offset < 0:  # pre-open ticks belong to the first bucket
        return open_dt
    return open_dt + timedelta(seconds=(int(offset) // secs) * secs)


def bucket_end(start: datetime, tf: str) -> datetime:
    start = to_ist(start)
    close_dt = datetime.combine(start.date(), SESSION_CLOSE, IST)
    if tf == "1d":
        return close_dt
    return min(start + timedelta(seconds=seconds(tf)), close_dt)


def is_higher(tf: str, base: str) -> bool:
    return seconds(tf) > seconds(base)
