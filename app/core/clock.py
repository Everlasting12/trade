"""Time handling. Everything internal is tz-aware IST (India has no DST, so a fixed offset is exact)."""

from datetime import date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")

# NSE/NFO/BSE equity & derivatives session. MCX/CDS use different hours (not handled in MVP).
SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)


def to_ist(ts: datetime) -> datetime:
    """Kite returns naive IST datetimes for ticks and tz-aware ones for historical candles."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=IST)
    return ts.astimezone(IST)


def now_ist() -> datetime:
    return datetime.now(IST)


def session_bounds(d: date) -> tuple[datetime, datetime]:
    return datetime.combine(d, SESSION_OPEN, IST), datetime.combine(d, SESSION_CLOSE, IST)


def is_market_hours(ts: datetime) -> bool:
    ts = to_ist(ts)
    if ts.weekday() >= 5:
        return False
    return SESSION_OPEN <= ts.time() < SESSION_CLOSE


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


class Clock:
    """Injectable clock so backtests run on simulated time and live runs on wall time."""

    def now(self) -> datetime:
        return now_ist()


class SimClock(Clock):
    def __init__(self, start: datetime | None = None):
        self._now = start or now_ist()

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        self._now = to_ist(ts)
