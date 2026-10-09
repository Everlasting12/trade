"""Builds typed config objects and strategies from the YAML trading config."""

from __future__ import annotations

from app.config.settings import Settings
from app.core.models import Product
from app.execution.paper.broker import PaperConfig
from app.risk.manager import RiskConfig
from app.signals.scoring import ScoringConfig
from app.strategies.base import Strategy
from app.strategies.library import create as create_strategy


def risk_config(cfg: dict, settings: Settings | None = None) -> RiskConfig:
    return RiskConfig.from_dict(cfg.get("risk"), **(settings.risk_overrides() if settings else {}))


def scoring_configs(cfg: dict) -> tuple[ScoringConfig, dict[str, ScoringConfig]]:
    base = cfg.get("scoring") or {}
    default = ScoringConfig.from_dict(base)
    overrides = {}
    for sid, o in (cfg.get("scoring_overrides") or {}).items():
        merged = {**base, **o, "weights": {**(base.get("weights") or {}), **(o.get("weights") or {})}}
        overrides[sid] = ScoringConfig.from_dict(merged)
    return default, overrides


def paper_config(cfg: dict, capital: float) -> PaperConfig:
    known = set(PaperConfig.__dataclass_fields__)
    return PaperConfig(capital=capital, **{k: v for k, v in (cfg.get("paper") or {}).items() if k in known})


def options_configs(cfg: dict):
    """Global options config + per-strategy overrides (strategy entries may carry an `options:` block)."""
    from app.options.planner import OptionsConfig
    base = OptionsConfig.from_dict(cfg.get("options"))
    overrides = {s["id"]: s["options"] for s in (cfg.get("strategies") or []) if s.get("options") and s.get("id")}
    return base, overrides


def default_product(cfg: dict) -> Product:
    return Product((cfg.get("engine") or {}).get("default_product", "MIS"))


def strategies(cfg: dict, default_timeframe: str = "5m") -> list[Strategy]:
    out = []
    for s in cfg.get("strategies") or []:
        out.append(create_strategy(
            s["type"], instruments=s["instruments"], timeframe=s.get("timeframe", default_timeframe),
            params=s.get("params"), strategy_id=s.get("id"), enabled=s.get("enabled", True),
            higher_timeframes=s.get("higher_timeframes"), execution=s.get("execution", "underlying")))
    ids = [s.id for s in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate strategy ids in config: {ids}")
    return out
