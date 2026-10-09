"""SQLite persistence (WAL mode). One file, safe for one writer process + concurrent readers.

For production, the same schema maps onto PostgreSQL (+ TimescaleDB hypertable for `candles`).

Tables
  orders    every order and its latest state (upserted on each change)
  fills     every execution
  signals   every signal with score and risk decision (also rejected/ignored ones)
  trades    closed round trips
  events    structured audit log
  candles   historical candle cache (instrument_token, timeframe, ts)
  state     key/value JSON for recovery (kill switch, paper account, OMS brackets, strategy toggles)
  journal   one row per position: why/when/how it was entered and exited (the trade journal)
  watchlists / watchlist_items   user watchlists (shared by paper and live)
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from app.core.models import Candle, Fill, Order

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    client_order_id TEXT PRIMARY KEY,
    broker_order_id TEXT,
    mode TEXT NOT NULL,
    strategy TEXT,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    product TEXT NOT NULL,
    role TEXT NOT NULL,
    price REAL,
    trigger_price REAL,
    status TEXT NOT NULL,
    filled_qty INTEGER NOT NULL DEFAULT 0,
    avg_fill_price REAL NOT NULL DEFAULT 0,
    parent_id TEXT,
    signal_id TEXT,
    reject_reason TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_orders_mode_status ON orders(mode, status);
CREATE INDEX IF NOT EXISTS ix_orders_created ON orders(created_at);

CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    client_order_id TEXT NOT NULL REFERENCES orders(client_order_id),
    mode TEXT NOT NULL,
    strategy TEXT,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    charges REAL NOT NULL DEFAULT 0,
    role TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fills_ts ON fills(ts);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    ts TEXT,
    strategy TEXT NOT NULL,
    instrument TEXT NOT NULL,
    timeframe TEXT,
    action TEXT NOT NULL,
    entry_price REAL, stop_loss REAL, target REAL, quantity INTEGER,
    score REAL, grade TEXT,
    status TEXT NOT NULL,            -- ignored_low_score | risk_rejected | approved | order_failed
    reasons TEXT,                    -- JSON list (strategy reasons)
    risk TEXT,                       -- JSON risk decision
    score_breakdown TEXT             -- JSON
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    position_id TEXT,
    strategy TEXT,
    instrument TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    entry_price REAL NOT NULL, exit_price REAL NOT NULL,
    entry_ts TEXT NOT NULL, exit_ts TEXT NOT NULL,
    gross_pnl REAL NOT NULL, charges REAL NOT NULL, net_pnl REAL NOT NULL,
    exit_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_exit ON trades(exit_ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    mode TEXT,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    strategy TEXT, instrument TEXT, action TEXT, reason TEXT,
    order_id TEXT, position_id TEXT, error TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS candles (
    instrument_token INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    ts TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume REAL NOT NULL DEFAULT 0, oi REAL,
    PRIMARY KEY (instrument_token, timeframe, ts)
);

CREATE TABLE IF NOT EXISTS journal (
    position_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | closed
    strategy TEXT,
    instrument TEXT NOT NULL,        -- traded contract, e.g. NFO:NIFTY26O1325000CE
    underlying TEXT,                 -- e.g. NIFTY
    option_type TEXT,                -- CE | PE | NULL (non-option)
    action TEXT,                     -- BUY_CE | BUY_PE | SELL_CE | SELL_PE | BUY | SELL
    direction TEXT,                  -- bullish | bearish (view on the underlying)
    strike REAL, expiry TEXT, dte REAL,
    lot_size INTEGER, lots INTEGER, quantity INTEGER,
    entry_time TEXT, entry_price REAL,
    stop_loss REAL, target REAL,     -- premium levels
    underlying_entry REAL, underlying_sl REAL, underlying_target REAL,
    planned_rr REAL, planned_risk REAL,
    iv REAL, delta REAL, atm_iv REAL, realized_vol REAL, adx REAL,
    score REAL, confidence REAL, timeframe TEXT,
    reasons TEXT, decision_notes TEXT,   -- JSON lists
    entry_charges REAL,
    exit_time TEXT, exit_price REAL, exit_reason TEXT, exit_quantity INTEGER,
    underlying_exit REAL,
    gross_pnl REAL, charges REAL, net_pnl REAL, r_multiple REAL,
    mfe REAL, mae REAL, duration_s INTEGER
);
CREATE INDEX IF NOT EXISTS ix_journal_entry ON journal(entry_time);

CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    position INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    instrument_key TEXT NOT NULL,          -- EXCHANGE:TRADINGSYMBOL
    token INTEGER NOT NULL,
    exchange TEXT, tradingsymbol TEXT, name TEXT, instrument_type TEXT, segment TEXT,
    expiry TEXT, strike REAL, lot_size INTEGER,
    note TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL,
    PRIMARY KEY (watchlist_id, instrument_key)
);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, default=str)


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


class Repository:
    def __init__(self, path: str | Path, mode: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._query("PRAGMA table_info(signals)")}
        if "meta" not in cols:
            self._conn.execute("ALTER TABLE signals ADD COLUMN meta TEXT")

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: Iterable = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    # ---- writes
    def save_order(self, o: Order) -> None:
        self._exec("""INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(client_order_id) DO UPDATE SET broker_order_id=excluded.broker_order_id,
              quantity=excluded.quantity, price=excluded.price, trigger_price=excluded.trigger_price,
              order_type=excluded.order_type, status=excluded.status, filled_qty=excluded.filled_qty,
              avg_fill_price=excluded.avg_fill_price, reject_reason=excluded.reject_reason,
              updated_at=excluded.updated_at""",
                   (o.client_order_id, o.broker_order_id, self.mode, o.strategy, o.instrument, str(o.side), o.quantity,
                    str(o.order_type), str(o.product), str(o.role), o.price, o.trigger_price, str(o.status),
                    o.filled_qty, o.avg_fill_price, o.parent_id, o.signal_id, o.reject_reason,
                    _iso(o.created_at), _iso(o.updated_at)))

    def save_fill(self, f: Fill) -> None:
        self._exec("INSERT OR IGNORE INTO fills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (f.fill_id, f.client_order_id, self.mode, f.strategy, f.instrument, str(f.side), f.quantity,
                    f.price, f.charges, str(f.role), _iso(f.ts)))

    def save_signal(self, rec: dict) -> None:
        self._exec("INSERT OR REPLACE INTO signals (signal_id, mode, ts, strategy, instrument, timeframe, action, "
                   "entry_price, stop_loss, target, quantity, score, grade, status, reasons, risk, score_breakdown, meta) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rec["signal_id"], self.mode, _iso(rec.get("ts")) if isinstance(rec.get("ts"), datetime) else rec.get("ts"),
                    rec["strategy"], rec["instrument"], rec.get("timeframe"),
                    str(rec["action"]), rec.get("entry_price"), rec.get("stop_loss"), rec.get("target"), rec.get("quantity"),
                    rec.get("score"), rec.get("grade"), rec["status"], _j(rec.get("reason")), _j(rec.get("risk")),
                    _j(rec.get("score_breakdown")), _j(rec.get("metadata"))))

    def save_trade(self, t) -> None:
        self._exec("INSERT OR REPLACE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (t.trade_id, self.mode, t.position_id, t.strategy, t.instrument, t.side, t.quantity, t.entry_price,
                    t.exit_price, _iso(t.entry_ts), _iso(t.exit_ts), t.gross_pnl, t.charges, t.net_pnl, t.exit_reason))

    def save_event(self, rec: dict) -> None:
        self._exec("INSERT INTO events (ts, mode, level, event, strategy, instrument, action, reason, order_id, "
                   "position_id, error, data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rec["ts"], rec.get("mode"), rec["level"], rec["event"], rec.get("strategy"), rec.get("instrument"),
                    rec.get("action"), rec.get("reason"), rec.get("order_id"), rec.get("position_id"), rec.get("error"),
                    _j(rec.get("data"))))

    # ---- watchlists
    def watchlists(self) -> list[dict]:
        return self._query("""SELECT w.id, w.name, w.position, COUNT(i.instrument_key) AS count
                               FROM watchlists w LEFT JOIN watchlist_items i ON i.watchlist_id = w.id
                               GROUP BY w.id ORDER BY w.position, w.id""")

    def create_watchlist(self, name: str) -> int:
        with self._lock:
            pos = self._conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM watchlists").fetchone()[0]
            cur = self._conn.execute("INSERT INTO watchlists (name, position, created_at) VALUES (?,?,?)",
                                     (name, pos, datetime.now().isoformat()))
            return cur.lastrowid

    def rename_watchlist(self, wid: int, name: str) -> None:
        self._exec("UPDATE watchlists SET name=? WHERE id=?", (name, wid))

    def delete_watchlist(self, wid: int) -> None:
        self._exec("DELETE FROM watchlist_items WHERE watchlist_id=?", (wid,))
        self._exec("DELETE FROM watchlists WHERE id=?", (wid,))

    def watchlist_items(self, wid: int) -> list[dict]:
        return self._query("SELECT * FROM watchlist_items WHERE watchlist_id=? ORDER BY position, added_at", (wid,))

    def all_watchlist_tokens(self) -> list[int]:
        return [r["token"] for r in self._query("SELECT DISTINCT token FROM watchlist_items")]

    def add_watchlist_items(self, wid: int, items: list[dict]) -> int:
        """Adds instruments (dicts with key/token/exchange/...); existing ones are skipped. Returns # added."""
        added = 0
        with self._lock:
            pos = self._conn.execute("SELECT COALESCE(MAX(position), 0) FROM watchlist_items WHERE watchlist_id=?",
                                     (wid,)).fetchone()[0]
            for it in items:
                pos += 1
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO watchlist_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (wid, it["key"], it["token"], it.get("exchange"), it.get("tradingsymbol"), it.get("name"),
                     it.get("instrument_type"), it.get("segment"), str(it["expiry"]) if it.get("expiry") else None,
                     it.get("strike"), it.get("lot_size"), it.get("note"), pos, datetime.now().isoformat()))
                added += cur.rowcount
        return added

    def remove_watchlist_items(self, wid: int, keys: list[str]) -> None:
        with self._lock:
            self._conn.executemany("DELETE FROM watchlist_items WHERE watchlist_id=? AND instrument_key=?",
                                   [(wid, k) for k in keys])

    # ---- trade journal
    _JOURNAL_JSON = ("reasons", "decision_notes")

    def journal_open(self, row: dict) -> None:
        row = {**row, "mode": self.mode, "status": "open"}
        for k in self._JOURNAL_JSON:
            row[k] = _j(row.get(k))
        cols = ", ".join(row)
        self._exec(f"INSERT OR REPLACE INTO journal ({cols}) VALUES ({', '.join('?' * len(row))})", row.values())

    def journal_close(self, position_id: str, ex: dict) -> None:
        """Accumulates partial exits; marks the row closed when the position is flat."""
        rows = self._query("SELECT * FROM journal WHERE position_id=?", (position_id,))
        if not rows:
            return
        r = rows[0]
        prev_q = r.get("exit_quantity") or 0
        q = prev_q + (ex.get("exit_quantity") or 0)
        exit_price = ((r.get("exit_price") or 0) * prev_q + ex["exit_price"] * ex["exit_quantity"]) / q if q else ex["exit_price"]
        gross = (r.get("gross_pnl") or 0) + (ex.get("gross_pnl") or 0)
        charges = (r.get("charges") or 0) + (ex.get("charges") or 0)
        net = (r.get("net_pnl") or 0) + (ex.get("net_pnl") or 0)
        risk = r.get("planned_risk")
        self._exec("""UPDATE journal SET status=?, exit_time=?, exit_price=?, exit_reason=?, exit_quantity=?,
                      underlying_exit=?, gross_pnl=?, charges=?, net_pnl=?, r_multiple=?, mfe=?, mae=?, duration_s=?
                      WHERE position_id=?""",
                   ("closed" if ex.get("closed", True) else "open", ex["exit_time"], round(exit_price, 2),
                    ex.get("exit_reason"), q, ex.get("underlying_exit"), round(gross, 2), round(charges, 2),
                    round(net, 2), round(net / risk, 2) if risk else None, ex.get("mfe"), ex.get("mae"),
                    ex.get("duration_s"), position_id))

    def journal(self, limit: int = 1000, since: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM journal WHERE mode=?", [self.mode]
        if since:
            sql += " AND entry_time>=?"
            args.append(since)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self._query(sql + " ORDER BY entry_time DESC LIMIT ?", (*args, limit))
        for r in rows:
            for k in self._JOURNAL_JSON:
                r[k] = json.loads(r[k]) if r.get(k) else None
        return rows

    def save_state(self, key: str, value: Any) -> None:
        self._exec("INSERT OR REPLACE INTO state VALUES (?,?,?)", (f"{self.mode}:{key}", _j(value),
                                                                     datetime.now().isoformat()))

    def load_state(self, key: str) -> Any:
        rows = self._query("SELECT value FROM state WHERE key=?", (f"{self.mode}:{key}",))
        return json.loads(rows[0]["value"]) if rows else None

    def save_candles(self, token: int, timeframe: str, candles: Iterable[Candle]) -> None:
        with self._lock:
            self._conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)",
                                   [(token, timeframe, c.ts.isoformat(), c.open, c.high, c.low, c.close, c.volume, c.oi)
                                    for c in candles])

    # ---- reads
    def load_candles(self, token: int, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        rows = self._query("SELECT * FROM candles WHERE instrument_token=? AND timeframe=? AND ts>=? AND ts<=? ORDER BY ts",
                           (token, timeframe, start.isoformat(), end.isoformat()))
        return [Candle(datetime.fromisoformat(r["ts"]), r["open"], r["high"], r["low"], r["close"], r["volume"],
                       r["oi"], timeframe) for r in rows]

    def orders(self, limit: int = 500, status: str | None = None) -> list[dict]:
        if status:
            return self._query("SELECT * FROM orders WHERE mode=? AND status=? ORDER BY created_at DESC LIMIT ?",
                               (self.mode, status, limit))
        return self._query("SELECT * FROM orders WHERE mode=? ORDER BY created_at DESC LIMIT ?", (self.mode, limit))

    def trades(self, limit: int = 1000, since: str | None = None) -> list[dict]:
        if since:
            return self._query("SELECT * FROM trades WHERE mode=? AND exit_ts>=? ORDER BY exit_ts DESC LIMIT ?",
                               (self.mode, since, limit))
        return self._query("SELECT * FROM trades WHERE mode=? ORDER BY exit_ts DESC LIMIT ?", (self.mode, limit))

    def signals(self, limit: int = 500) -> list[dict]:
        rows = self._query("SELECT * FROM signals WHERE mode=? ORDER BY ts DESC LIMIT ?", (self.mode, limit))
        for r in rows:
            for k in ("reasons", "risk", "score_breakdown", "meta"):
                r[k] = json.loads(r[k]) if r.get(k) else None
        return rows

    def events(self, limit: int = 500, level: str | None = None) -> list[dict]:
        if level:
            rows = self._query("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit))
        else:
            rows = self._query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["data"] = json.loads(r["data"]) if r.get("data") else None
        return rows
