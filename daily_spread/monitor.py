from datetime import datetime, timezone
from typing import Dict, List, Optional

from .audit import AuditLog
from .config import Settings
from .execute import Executor
from .guards import ReentryLock
from .market import MarketData
from .rules import RiskRules


class PositionMonitor:
    def __init__(self, settings: Settings, rules: RiskRules, executor: Executor,
                 market: MarketData, audit: AuditLog, lock: ReentryLock):
        self.settings = settings
        self.rules = rules
        self.executor = executor
        self.market = market
        self.audit = audit
        self.lock = lock

    def stop_multiple(self, entry: Dict) -> float:
        if self.rules.stop_mode != "volatility_scaled":
            return self.rules.fixed_stop_multiple

        iv = entry.get("short_iv")
        if not iv:
            return self.rules.fixed_stop_multiple

        scaled = self.rules.fixed_stop_multiple * (1.0 + (float(iv) - 0.20) * self.rules.iv_multiplier)
        return max(self.rules.min_stop_multiple, min(self.rules.max_stop_multiple, scaled))

    def next_tier(self, entry: Dict, profit_pct: float) -> Optional[Dict]:
        fired = set(entry.get("tiers_fired", []))
        for tier in self.rules.tiers:
            if tier.gain_pct in fired:
                continue
            if profit_pct >= tier.gain_pct:
                return {"gain_pct": tier.gain_pct, "close_fraction": tier.close_fraction}
        return None

    def review(self) -> List[str]:
        actions = []
        entries = [e for e in self.executor.journal.load() if e.get("status") == "open"]
        if not entries:
            return ["no open spreads"]

        live = {p.get('symbol'): p for p in self.executor.option_positions()}

        for entry in entries:
            short_symbol = entry["short_symbol"]
            long_symbol = entry["long_symbol"]
            underlying = entry["underlying"]

            if short_symbol not in live and long_symbol not in live:
                self.executor.journal.update_status(entry["order_id"], "closed")
                self.lock.record_close(underlying, closed_at_gain=True)
                actions.append(f"{underlying} no longer held, journal reconciled")
                continue

            current = self._current_debit(entry, live)
            if current is None:
                actions.append(f"{underlying} no mark available")
                continue

            credit = float(entry["credit"])
            profit_pct = (credit - current) / credit if credit > 0 else 0.0
            dte = self._dte(short_symbol)
            remaining = int(entry.get("contracts_remaining", entry.get("contracts", 0)))

            if remaining <= 0:
                self.executor.journal.update_status(entry["order_id"], "closed")
                continue

            if dte is not None and dte <= self.rules.close_at_dte:
                if self.executor.close_spread(entry, remaining):
                    self.lock.record_close(underlying, closed_at_gain=profit_pct > 0)
                    self.audit.order("live" if self.rules.is_live else "dry_run", "close",
                                     underlying, reason="expiry_guard", dte=dte,
                                     contracts=remaining, profit_pct=round(profit_pct, 4))
                    actions.append(f"{underlying} closed on {dte} dte expiry guard")
                continue

            multiple = self.stop_multiple(entry)
            if current >= credit * multiple:
                if self.executor.close_spread(entry, remaining):
                    self.lock.record_close(underlying, closed_at_gain=False)
                    self.audit.order("live" if self.rules.is_live else "dry_run", "close",
                                     underlying, reason="stop_loss", contracts=remaining,
                                     stop_multiple=round(multiple, 3),
                                     mark=current, credit=credit)
                    actions.append(
                        f"{underlying} stopped out at {current:.2f} vs credit {credit:.2f} "
                        f"({multiple:.2f}x)")
                continue

            tier = self.next_tier(entry, profit_pct)
            if tier:
                quantity = max(1, int(round(remaining * tier["close_fraction"])))
                quantity = min(quantity, remaining)
                if self.executor.close_spread(entry, quantity):
                    self.executor.journal.record_tier(
                        entry["order_id"], tier["gain_pct"], quantity)
                    if quantity >= remaining:
                        self.lock.record_close(underlying, closed_at_gain=True)
                    self.audit.order("live" if self.rules.is_live else "dry_run", "close",
                                     underlying, reason="take_profit",
                                     tier=tier["gain_pct"], contracts=quantity,
                                     profit_pct=round(profit_pct, 4))
                    actions.append(
                        f"{underlying} tier {tier['gain_pct']:.0%} fired, closed {quantity} "
                        f"of {remaining} at {profit_pct:.0%} of credit")
                continue

            actions.append(
                f"{underlying} held x{remaining}, mark {current:.2f} vs credit {credit:.2f}, "
                f"p/l {profit_pct:+.0%}, dte {dte}, stop {multiple:.2f}x")

        return actions

    def _current_debit(self, entry: Dict, live: Dict) -> Optional[float]:
        short_position = live.get(entry["short_symbol"])
        long_position = live.get(entry["long_symbol"])
        if short_position is None or long_position is None:
            return None
        try:
            short_price = abs(float(short_position.get("current_price")))
            long_price = abs(float(long_position.get("current_price")))
            return round(short_price - long_price, 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _dte(option_symbol: str) -> Optional[int]:
        try:
            body = option_symbol[-15:]
            expiry = datetime(2000 + int(body[0:2]), int(body[2:4]), int(body[4:6]),
                              tzinfo=timezone.utc)
            return (expiry.date() - datetime.now(timezone.utc).date()).days
        except (ValueError, IndexError):
            return None
