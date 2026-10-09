"""Domain models shared by every layer (market data, strategies, risk, OMS, brokers, portfolio).

These are plain dataclasses with no I/O so that the same objects flow through backtests,
paper trading and live trading.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class SignalAction(StrEnum):
    BUY = "BUY"      # open/increase long
    SELL = "SELL"    # open/increase short
    EXIT = "EXIT"    # close the strategy's position in the instrument


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"        # stop-limit
    SLM = "SL-M"     # stop-market


class Product(StrEnum):
    MIS = "MIS"      # intraday
    NRML = "NRML"    # F&O carry-forward
    CNC = "CNC"      # equity delivery


class OrderRole(StrEnum):
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    EXIT = "EXIT"


class OrderStatus(StrEnum):
    CREATED = "CREATED"              # known to OMS, not yet sent
    SUBMITTED = "SUBMITTED"          # sent, broker ack pending
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"              # send outcome unknown (network error) -> must reconcile

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    @property
    def is_working(self) -> bool:
        return self in (OrderStatus.SUBMITTED, OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING,
                        OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Instrument:
    token: int
    exchange: str
    tradingsymbol: str
    instrument_type: str = "EQ"         # EQ, FUT, CE, PE, INDEX
    segment: str = ""
    name: str = ""                      # underlying name for derivatives, e.g. NIFTY
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: date | None = None
    strike: float | None = None

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ("CE", "PE")

    @property
    def is_future(self) -> bool:
        return self.instrument_type == "FUT"

    @property
    def is_derivative(self) -> bool:
        return self.is_option or self.is_future

    @property
    def tradable(self) -> bool:
        return self.instrument_type != "INDEX" and self.segment != "INDICES"

    def round_to_tick(self, price: float) -> float:
        ts = self.tick_size or 0.05
        return round(round(price / ts) * ts, 2)

    @classmethod
    def from_kite(cls, row: dict) -> "Instrument":
        expiry = row.get("expiry")
        if isinstance(expiry, str):
            expiry = date.fromisoformat(expiry) if expiry else None
        itype = row.get("instrument_type") or "EQ"
        if row.get("segment") == "INDICES":
            itype = "INDEX"
        return cls(
            token=int(row["instrument_token"]), exchange=row["exchange"], tradingsymbol=row["tradingsymbol"],
            instrument_type=itype, segment=row.get("segment") or "", name=row.get("name") or "",
            lot_size=int(row.get("lot_size") or 1), tick_size=float(row.get("tick_size") or 0.05),
            expiry=expiry or None, strike=float(row["strike"]) if row.get("strike") else None,
        )


@dataclass(slots=True)
class Tick:
    instrument_token: int
    ts: datetime                         # exchange timestamp (IST, tz-aware)
    ltp: float
    cumulative_volume: int | None = None # day volume (Kite volume_traded)
    oi: int | None = None
    bid: float | None = None
    ask: float | None = None
    bid_qty: int | None = None
    ask_qty: int | None = None
    day_open: float | None = None
    day_high: float | None = None
    day_low: float | None = None
    prev_close: float | None = None
    vwap: float | None = None            # Kite average_traded_price


@dataclass(slots=True)
class Candle:
    ts: datetime                         # bucket start (IST, tz-aware)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float | None = None
    timeframe: str = "1m"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Signal:
    action: SignalAction
    instrument: str                      # instrument key, e.g. "NFO:NIFTY26OCTFUT"
    strategy: str
    timeframe: str
    entry_price: float | None = None     # reference / limit price; None = market
    stop_loss: float | None = None
    target: float | None = None
    quantity: int | None = None          # None = let the position sizer decide
    confidence: float = 0.0              # 0..1, set by the scorer
    reason: list[str] = field(default_factory=list)
    ts: datetime | None = None           # close time of the candle that produced it
    entry_order_type: OrderType = OrderType.MARKET
    score: float | None = None
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    signal_id: str = field(default_factory=lambda: new_id("sig_"))
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def side(self) -> Side | None:
        return {SignalAction.BUY: Side.BUY, SignalAction.SELL: Side.SELL}.get(self.action)

    @property
    def risk_per_unit(self) -> float | None:
        if self.entry_price is None or self.stop_loss is None:
            return None
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk_reward(self) -> float | None:
        r = self.risk_per_unit
        if not r or self.target is None or self.entry_price is None:
            return None
        return round(abs(self.target - self.entry_price) / r, 2)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["risk_reward"] = self.risk_reward
        return d


@dataclass(slots=True)
class Order:
    instrument: str
    side: Side
    quantity: int
    order_type: OrderType
    product: Product
    role: OrderRole
    strategy: str
    price: float | None = None
    trigger_price: float | None = None
    signal_id: str | None = None
    parent_id: str | None = None         # entry order id for protective orders
    client_order_id: str = field(default_factory=lambda: new_id("o"))
    broker_order_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    reject_reason: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    mode: str = ""

    @property
    def remaining_qty(self) -> int:
        return self.quantity - self.filled_qty

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class OrderUpdate:
    """Broker -> OMS notification. Fills carry `fill_qty`/`fill_price` for the increment."""
    client_order_id: str
    status: OrderStatus
    ts: datetime
    broker_order_id: str | None = None
    filled_qty: int = 0                  # cumulative
    avg_fill_price: float = 0.0
    fill_qty: int = 0                    # this increment
    fill_price: float = 0.0
    charges: float = 0.0
    reject_reason: str | None = None


@dataclass(slots=True)
class Fill:
    client_order_id: str
    instrument: str
    side: Side
    quantity: int
    price: float
    ts: datetime
    charges: float = 0.0
    strategy: str = ""
    role: OrderRole = OrderRole.ENTRY
    fill_id: str = field(default_factory=lambda: new_id("f_"))

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class BrokerPosition:
    instrument: str
    quantity: int                        # signed
    avg_price: float
    last_price: float = 0.0
    pnl: float = 0.0
    product: str = ""


@dataclass(slots=True)
class AccountBalance:
    cash: float
    available_margin: float
    used_margin: float
    realized_pnl: float = 0.0
    charges: float = 0.0
