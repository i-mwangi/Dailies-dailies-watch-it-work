import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings
from .rules import RiskRules


@dataclass
class LossLimitStatus:
    daily_pnl: float
    weekly_pnl: float
    daily_cap: float
    weekly_cap: float
    entries_halted: bool
    reason: str

    def to_dict(self) -> Dict:
        return {
            "daily_pnl": round(self.daily_pnl, 2),
            "weekly_pnl": round(self.weekly_pnl, 2),
            "daily_cap": round(self.daily_cap, 2),
            "weekly_cap": round(self.weekly_cap, 2),
            "entries_halted": self.entries_halted,
            "reason": self.reason,
        }


class AnchorStore:
    def __init__(self, settings: Settings):
        self.path = Path(settings.state_dir) / "anchors.json"

    def load(self) -> Dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def week_anchor(self, equity: float) -> float:
        data = self.load()
        now = datetime.now(timezone.utc)
        week_id = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"

        if data.get("week_id") != week_id:
            data["week_id"] = week_id
            data["week_start_equity"] = equity
            self.save(data)
            return equity

        return float(data.get("week_start_equity", equity))


class LossLimitGuard:
    def __init__(self, settings: Settings, rules: RiskRules):
        self.settings = settings
        self.rules = rules
        self.anchors = AnchorStore(settings)

    def evaluate(self, equity: float, last_equity: float) -> LossLimitStatus:
        daily_cap = self.rules.daily_loss_cap()
        weekly_cap = self.rules.weekly_loss_cap()

        week_start = self.anchors.week_anchor(equity)

        daily_pnl = equity - last_equity if last_equity > 0 else 0.0
        weekly_pnl = equity - week_start

        reasons = []
        if daily_pnl < 0 and abs(daily_pnl) >= daily_cap:
            reasons.append(
                f"daily loss {abs(daily_pnl):.2f} reached cap {daily_cap:.2f}")
        if weekly_pnl < 0 and abs(weekly_pnl) >= weekly_cap:
            reasons.append(
                f"weekly loss {abs(weekly_pnl):.2f} reached cap {weekly_cap:.2f}")

        return LossLimitStatus(
            daily_pnl=daily_pnl,
            weekly_pnl=weekly_pnl,
            daily_cap=daily_cap,
            weekly_cap=weekly_cap,
            entries_halted=bool(reasons),
            reason="; ".join(reasons) if reasons else "within limits",
        )


class ReentryLock:
    def __init__(self, settings: Settings, rules: RiskRules):
        self.rules = rules
        self.path = Path(settings.state_dir) / "reentry_locks.json"

    def load(self) -> Dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self, data: Dict) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record_close(self, underlying: str, closed_at_gain: bool) -> None:
        if not self.rules.reentry_enabled:
            return

        hours = self.rules.gain_lock_hours if closed_at_gain else self.rules.loss_lock_hours
        until = datetime.now(timezone.utc) + timedelta(hours=hours)

        data = self.load()
        data[underlying] = {
            "locked_until": until.isoformat(),
            "closed_at_gain": closed_at_gain,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        self.save(data)

    def is_locked(self, underlying: str) -> bool:
        return self.remaining_hours(underlying) is not None

    def remaining_hours(self, underlying: str) -> Optional[float]:
        if not self.rules.reentry_enabled:
            return None

        entry = self.load().get(underlying)
        if not entry:
            return None

        try:
            until = datetime.fromisoformat(entry["locked_until"])
        except (KeyError, ValueError):
            return None

        remaining = (until - datetime.now(timezone.utc)).total_seconds() / 3600.0
        return remaining if remaining > 0 else None

    def active(self) -> List[str]:
        return [symbol for symbol in self.load() if self.is_locked(symbol)]


@dataclass
class ProtectionLock:
    locked: bool
    until: Optional[str]
    reason: str
    source: str

    def to_dict(self) -> Dict:
        return {
            "locked": self.locked,
            "until": self.until,
            "reason": self.reason,
            "source": self.source,
        }


class StopLossGuard:
    def __init__(self, settings: Settings, rules: RiskRules, audit):
        self.settings = settings
        self.rules = rules
        self.audit = audit

    def evaluate(self) -> ProtectionLock:
        config = self.rules.stoploss_guard
        if not config or not config.enabled:
            return ProtectionLock(False, None, "disabled", "stoploss_guard")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=config.lookback_hours)
        stops = []

        for record in self.audit.read(stage="order"):
            if record.get("reason") != "stop_loss":
                continue
            stamp = f"{record.get('date')}T{record.get('timestamp')}+00:00"
            try:
                when = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if when >= cutoff:
                stops.append(when)

        if len(stops) < config.trade_limit:
            return ProtectionLock(
                False, None,
                f"{len(stops)} stop-outs in {config.lookback_hours:.0f}h, "
                f"limit {config.trade_limit}",
                "stoploss_guard")

        until = max(stops) + timedelta(hours=config.stop_duration_hours)
        if until <= datetime.now(timezone.utc):
            return ProtectionLock(False, None, "lock expired", "stoploss_guard")

        return ProtectionLock(
            True, until.isoformat(),
            f"{len(stops)} stop-outs within {config.lookback_hours:.0f}h "
            f"reached limit {config.trade_limit}, entries locked until {until:%Y-%m-%d %H:%M} UTC",
            "stoploss_guard")


class DrawdownGuard:
    def __init__(self, settings: Settings, rules: RiskRules):
        self.settings = settings
        self.rules = rules
        self.anchors = AnchorStore(settings)

    def evaluate(self, equity: float) -> ProtectionLock:
        config = self.rules.max_drawdown
        if not config or not config.enabled:
            return ProtectionLock(False, None, "disabled", "max_drawdown")

        data = self.anchors.load()
        peak = float(data.get("peak_equity", 0) or 0)

        if equity > peak:
            data["peak_equity"] = equity
            data["peak_recorded_at"] = datetime.now(timezone.utc).isoformat()
            self.anchors.save(data)
            peak = equity

        if peak <= 0:
            return ProtectionLock(False, None, "no peak recorded", "max_drawdown")

        drawdown = (peak - equity) / peak

        if drawdown < config.max_allowed_drawdown_pct:
            return ProtectionLock(
                False, None,
                f"drawdown {drawdown:.2%} from peak {peak:.2f}, "
                f"limit {config.max_allowed_drawdown_pct:.2%}",
                "max_drawdown")

        until = datetime.now(timezone.utc) + timedelta(hours=config.stop_duration_hours)
        return ProtectionLock(
            True, until.isoformat(),
            f"drawdown {drawdown:.2%} from peak {peak:.2f} exceeded "
            f"{config.max_allowed_drawdown_pct:.2%}, entries locked until "
            f"{until:%Y-%m-%d %H:%M} UTC",
            "max_drawdown")
