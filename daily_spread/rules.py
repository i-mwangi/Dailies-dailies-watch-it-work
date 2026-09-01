import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .config import ROOT, Settings

RULES_PATH = ROOT / "risk_rules.json"


class RulesError(RuntimeError):
    pass


@dataclass
class ProfitTier:
    gain_pct: float
    close_fraction: float


@dataclass
class ProtectionConfig:
    enabled: bool
    lookback_hours: float
    trade_limit: int
    stop_duration_hours: float
    max_allowed_drawdown_pct: float


@dataclass
class RiskRules:
    account_number: str
    starting_capital: float
    mode: str
    min_cycles_before_live: int
    daily_loss_limit_pct: float
    weekly_loss_limit_pct: float
    stop_mode: str
    fixed_stop_multiple: float
    iv_multiplier: float
    min_stop_multiple: float
    max_stop_multiple: float
    close_at_dte: int
    reentry_enabled: bool
    gain_lock_hours: float
    loss_lock_hours: float
    max_contracts_per_spread: int
    interval_minutes: int
    stoploss_guard: Optional[ProtectionConfig] = None
    max_drawdown: Optional[ProtectionConfig] = None
    tiers: List[ProfitTier] = field(default_factory=list)
    raw: Dict = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def daily_loss_cap(self) -> float:
        return self.starting_capital * self.daily_loss_limit_pct

    def weekly_loss_cap(self) -> float:
        return self.starting_capital * self.weekly_loss_limit_pct


def load_rules(path: Path = RULES_PATH) -> RiskRules:
    if not path.exists():
        raise RulesError(f"risk rules file not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RulesError(f"risk rules file is not valid JSON: {exc}")

    execution = data.get("execution", {})
    mode = str(execution.get("mode", "dry_run")).strip().lower()
    if mode not in ("dry_run", "live"):
        raise RulesError(f"execution.mode must be 'dry_run' or 'live', found '{mode}'")

    limits = data.get("loss_limits", {})
    stop = data.get("stop_loss", {})
    lock = data.get("reentry_lock", {})
    sizing = data.get("position_sizing", {})

    tiers = []
    for tier in data.get("take_profit", {}).get("tiers", []):
        tiers.append(ProfitTier(
            gain_pct=float(tier["gain_pct"]),
            close_fraction=float(tier["close_fraction"]),
        ))
    tiers.sort(key=lambda t: t.gain_pct)

    protections = data.get("protections", {})
    guard_raw = protections.get("stoploss_guard", {})
    drawdown_raw = protections.get("max_drawdown", {})

    stoploss_guard = ProtectionConfig(
        enabled=bool(guard_raw.get("enabled", False)),
        lookback_hours=float(guard_raw.get("lookback_hours", 24)),
        trade_limit=int(guard_raw.get("trade_limit", 3)),
        stop_duration_hours=float(guard_raw.get("stop_duration_hours", 12)),
        max_allowed_drawdown_pct=0.0,
    )
    max_drawdown = ProtectionConfig(
        enabled=bool(drawdown_raw.get("enabled", False)),
        lookback_hours=float(drawdown_raw.get("lookback_hours", 0)),
        trade_limit=0,
        stop_duration_hours=float(drawdown_raw.get("stop_duration_hours", 12)),
        max_allowed_drawdown_pct=float(drawdown_raw.get("max_allowed_drawdown_pct", 0.08)),
    )

    return RiskRules(
        account_number=str(data.get("account_number", "")),
        starting_capital=float(data.get("starting_capital_usd", 0.0)),
        mode=mode,
        min_cycles_before_live=int(execution.get("dry_run_min_cycles_before_live", 0)),
        daily_loss_limit_pct=float(limits.get("daily_loss_limit_pct_of_account", 0.05)),
        weekly_loss_limit_pct=float(limits.get("weekly_loss_limit_pct_of_account", 0.10)),
        stop_mode=str(stop.get("mode", "fixed")).strip().lower(),
        fixed_stop_multiple=float(stop.get("fixed_stop_multiple", 2.0)),
        iv_multiplier=float(stop.get("iv_multiplier", 1.10)),
        min_stop_multiple=float(stop.get("min_stop_multiple", 1.5)),
        max_stop_multiple=float(stop.get("max_stop_multiple", 3.0)),
        close_at_dte=int(data.get("expiry_guard", {}).get("close_at_dte", 2)),
        reentry_enabled=bool(lock.get("enabled", True)),
        gain_lock_hours=float(lock.get("gain_close_lock_hours", 48)),
        loss_lock_hours=float(lock.get("loss_close_lock_hours", 120)),
        max_contracts_per_spread=int(sizing.get("max_contracts_per_spread", 10)),
        interval_minutes=int(data.get("cadence", {}).get("default_interval_minutes", 75)),
        stoploss_guard=stoploss_guard,
        max_drawdown=max_drawdown,
        tiers=tiers,
        raw=data,
    )


def apply_rules(settings: Settings, rules: RiskRules) -> Settings:
    data = rules.raw
    signal = data.get("signal", {})
    sizing = data.get("position_sizing", {})
    construction = data.get("spread_construction", {})

    settings.min_conviction = float(signal.get("min_conviction", settings.min_conviction))
    settings.min_articles_per_sector = int(
        signal.get("min_articles_per_sector", settings.min_articles_per_sector))
    settings.news_lookback_hours = int(
        signal.get("news_lookback_hours", settings.news_lookback_hours))

    settings.max_risk_per_trade_pct = float(
        sizing.get("max_risk_per_trade_pct", settings.max_risk_per_trade_pct))
    settings.max_total_risk_pct = float(
        sizing.get("max_total_risk_pct", settings.max_total_risk_pct))
    settings.min_cash_buffer_pct = float(
        sizing.get("min_cash_buffer_pct", settings.min_cash_buffer_pct))
    settings.max_open_positions = int(
        sizing.get("max_open_positions", settings.max_open_positions))
    settings.max_positions_per_underlying = int(
        sizing.get("max_positions_per_underlying", settings.max_positions_per_underlying))

    settings.target_dte_min = int(construction.get("target_dte_min", settings.target_dte_min))
    settings.target_dte_max = int(construction.get("target_dte_max", settings.target_dte_max))
    settings.short_delta_target = float(
        construction.get("short_delta_target", settings.short_delta_target))
    settings.spread_width_min = float(
        construction.get("spread_width_min", settings.spread_width_min))
    settings.spread_width_max = float(
        construction.get("spread_width_max", settings.spread_width_max))
    settings.min_credit_to_width = float(
        construction.get("min_credit_to_width", settings.min_credit_to_width))

    settings.close_at_dte = rules.close_at_dte
    return settings
