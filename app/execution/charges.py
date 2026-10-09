"""Brokerage and statutory charges (Zerodha-style), all rates configurable.

Rates change (budget STT revisions, exchange fee changes). The defaults below are a best effort as of
2026 and MUST be verified against https://zerodha.com/charges before relying on paper/backtest P&L.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import Instrument, Product, Side


@dataclass
class SegmentRates:
    brokerage_pct: float          # of turnover, capped at brokerage_cap per executed order
    brokerage_cap: float
    stt_buy_pct: float
    stt_sell_pct: float
    exchange_pct: float
    stamp_buy_pct: float


@dataclass
class ChargesConfig:
    sebi_per_crore: float = 10.0
    gst_pct: float = 18.0
    rates: dict[str, SegmentRates] = field(default_factory=lambda: {
        # STT on futures/options per Union Budget 2026 revision (verify).
        "futures": SegmentRates(0.03, 20.0, 0.0, 0.05, 0.00173, 0.002),
        "options": SegmentRates(100.0, 20.0, 0.0, 0.15, 0.03503, 0.003),   # flat Rs 20/order (pct high -> cap applies)
        "equity_intraday": SegmentRates(0.03, 20.0, 0.0, 0.025, 0.00297, 0.003),
        "equity_delivery": SegmentRates(0.0, 0.0, 0.1, 0.1, 0.00297, 0.015),
    })

    def segment(self, inst: Instrument, product: Product | str) -> str:
        if inst.is_option:
            return "options"
        if inst.is_future:
            return "futures"
        return "equity_delivery" if str(product) == Product.CNC else "equity_intraday"


@dataclass
class ChargeBreakdown:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst, 2)


class ChargesModel:
    def __init__(self, config: ChargesConfig | None = None):
        self.cfg = config or ChargesConfig()

    def for_fill(self, inst: Instrument, product: Product | str, side: Side, qty: int, price: float,
                 order_turnover_before: float = 0.0) -> ChargeBreakdown:
        """Charges for one fill. `order_turnover_before` = turnover of earlier partial fills of the same
        order, so the per-order brokerage cap is applied across partial fills."""
        r = self.cfg.rates[self.cfg.segment(inst, product)]
        turnover = qty * price
        cap = r.brokerage_cap
        total_before = min(cap, order_turnover_before * r.brokerage_pct / 100) if cap else order_turnover_before * r.brokerage_pct / 100
        total_after = min(cap, (order_turnover_before + turnover) * r.brokerage_pct / 100) if cap else \
            (order_turnover_before + turnover) * r.brokerage_pct / 100
        brokerage = max(0.0, total_after - total_before)
        stt = turnover * (r.stt_buy_pct if side is Side.BUY else r.stt_sell_pct) / 100
        exchange = turnover * r.exchange_pct / 100
        sebi = turnover * self.cfg.sebi_per_crore / 1e7
        stamp = turnover * r.stamp_buy_pct / 100 if side is Side.BUY else 0.0
        gst = (brokerage + exchange + sebi) * self.cfg.gst_pct / 100
        return ChargeBreakdown(*(round(x, 4) for x in (brokerage, stt, exchange, sebi, stamp, gst)))
