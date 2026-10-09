"""Broker abstraction. The OMS talks only to this interface; strategies never see it.

Order lifecycle events (accepted, triggered, partial/complete fills, rejections, cancels) are pushed to
the OMS through the update handler, for both paper and live, so the OMS has one code path.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable

from app.core.models import AccountBalance, BrokerPosition, Instrument, Order, OrderUpdate, TradingMode

UpdateHandler = Callable[[OrderUpdate], None]


class BrokerError(Exception):
    """Broker/API failure. `retryable` is only ever True for operations that are safe to repeat."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OrderRejected(BrokerError):
    pass


class BrokerInterface(ABC):
    mode: TradingMode

    def __init__(self) -> None:
        self._handler: UpdateHandler | None = None

    def set_update_handler(self, handler: UpdateHandler) -> None:
        self._handler = handler

    def _emit(self, update: OrderUpdate) -> None:
        if self._handler:
            self._handler(update)

    @abstractmethod
    def place_order(self, order: Order) -> str:
        """Submit and return the broker order id. Must NOT be blindly retried by callers: on an
        ambiguous failure the OMS reconciles via get_orders() using the order tag."""

    @abstractmethod
    def modify_order(self, order: Order, *, quantity: int | None = None, price: float | None = None,
                     trigger_price: float | None = None, order_type: str | None = None) -> None: ...

    @abstractmethod
    def cancel_order(self, order: Order) -> None: ...

    @abstractmethod
    def get_order_status(self, order: Order) -> OrderUpdate | None: ...

    @abstractmethod
    def get_orders(self) -> list[OrderUpdate]: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def get_account_balance(self) -> AccountBalance: ...

    @abstractmethod
    def estimate_margin(self, order: Order, instrument: Instrument, price: float) -> float: ...
