"""Configuration.

- Secrets and deployment switches come from environment variables (.env locally; a secret manager in
  production). Never commit .env.
- Trading parameters (strategies, scoring, risk defaults, paper-fill model) come from a YAML file
  (config/trading.yaml) so they are reviewable and versioned. Risk limits listed in the environment
  (MAX_DAILY_LOSS, ...) override the YAML so ops can tighten them without a code/config change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from app.core.models import TradingMode

ROOT = Path(__file__).resolve().parents[2]
LIVE_CONFIRM_PHRASE = "I UNDERSTAND THIS PLACES REAL ORDERS"


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _num(name: str, cast=float):
    v = os.getenv(name)
    return cast(v) if v not in (None, "") else None


@dataclass(frozen=True)
class Settings:
    broker_api_key: str = ""
    broker_api_secret: str = ""
    access_token: str = ""
    trading_mode: TradingMode = TradingMode.PAPER
    live_trading_enabled: bool = False
    live_max_order_lots: int = 1
    feed_source: str = "kite"                 # kite | synthetic
    synthetic_speed: float = 10.0
    default_timeframe: str = "5m"
    default_capital: float = 500_000.0
    max_daily_loss: float | None = None
    max_risk_per_trade: float | None = None
    max_trades_per_day: int | None = None
    max_open_positions: int | None = None
    db_path: Path = ROOT / "data" / "trading.db"
    log_dir: Path = ROOT / "logs"
    trading_config: Path = ROOT / "config" / "trading.yaml"
    backend_url: str = "http://127.0.0.1:8000"
    frontend_url: str = "http://localhost:8501"
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(ROOT / ".env")
        mode = (os.getenv("TRADING_MODE") or "paper").strip().lower()
        if mode not in ("paper", "live"):
            raise ValueError(f"TRADING_MODE must be 'paper' or 'live', got {mode!r}")
        return cls(
            broker_api_key=os.getenv("BROKER_API_KEY") or os.getenv("KITE_API_KEY") or "",
            broker_api_secret=os.getenv("BROKER_API_SECRET") or os.getenv("KITE_API_SECRET") or "",
            access_token=os.getenv("ACCESS_TOKEN") or "",
            trading_mode=TradingMode(mode),
            live_trading_enabled=_bool(os.getenv("LIVE_TRADING_ENABLED"), False),
            live_max_order_lots=int(os.getenv("LIVE_MAX_ORDER_LOTS") or 1),
            feed_source=(os.getenv("FEED_SOURCE") or "kite").strip().lower(),
            synthetic_speed=float(os.getenv("SYNTHETIC_SPEED") or 10),
            default_timeframe=os.getenv("DEFAULT_TIMEFRAME") or "5m",
            default_capital=float(os.getenv("DEFAULT_CAPITAL") or 500_000),
            max_daily_loss=_num("MAX_DAILY_LOSS"),
            max_risk_per_trade=_num("MAX_RISK_PER_TRADE"),
            max_trades_per_day=_num("MAX_TRADES_PER_DAY", int),
            max_open_positions=_num("MAX_OPEN_POSITIONS", int),
            db_path=Path(os.getenv("DB_PATH") or ROOT / "data" / "trading.db"),
            log_dir=Path(os.getenv("LOG_DIR") or ROOT / "logs"),
            trading_config=Path(os.getenv("TRADING_CONFIG") or ROOT / "config" / "trading.yaml"),
            backend_url=os.getenv("BACKEND_URL") or "http://127.0.0.1:8000",
            frontend_url=os.getenv("FRONTEND_URL") or "http://localhost:8501",
        )

    def risk_overrides(self) -> dict:
        return {"max_daily_loss": self.max_daily_loss, "max_risk_per_trade": self.max_risk_per_trade,
                "max_trades_per_day": self.max_trades_per_day, "max_open_positions": self.max_open_positions}

    def public(self) -> dict:
        """Safe-to-display view (no secrets)."""
        return {"trading_mode": str(self.trading_mode), "live_trading_enabled": self.live_trading_enabled,
                "live_max_order_lots": self.live_max_order_lots, "feed_source": self.feed_source,
                "default_timeframe": self.default_timeframe, "default_capital": self.default_capital,
                "api_key_configured": bool(self.broker_api_key), "secret_configured": bool(self.broker_api_secret)}


def load_trading_config(path: Path | str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Trading config not found: {p}")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}
