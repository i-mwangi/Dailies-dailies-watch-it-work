import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .cli import AlpacaCLI, CLIError
from .config import Settings
from .market import VerticalSpread
from .risk import PortfolioState
from .rules import RiskRules
from .signal import Thesis


class ExecutionBlocked(RuntimeError):
    pass


@dataclass
class TradeRecord:
    order_id: str
    underlying: str
    strategy: str
    short_symbol: str
    long_symbol: str
    contracts: int
    contracts_remaining: int
    credit: float
    width: float
    max_loss: float
    short_iv: Optional[float]
    opened_at: str
    sector: str
    direction: str
    conviction: float
    thesis: str
    key_risk: str
    status: str = "open"
    order_status: str = "submitted"
    tiers_fired: List[float] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return self.__dict__.copy()


class Journal:
    def __init__(self, settings: Settings):
        self.path = Path(settings.state_dir) / "journal.json"

    def load(self) -> List[Dict]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, entries: List[Dict]) -> None:
        self.path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def append(self, record: Dict) -> None:
        entries = self.load()
        entries.append(record)
        self._save(entries)

    def update_status(self, order_id: str, status: str) -> None:
        entries = self.load()
        for entry in entries:
            if entry.get("order_id") == order_id:
                entry["status"] = status
                entry["closed_at"] = datetime.now(timezone.utc).isoformat()
        self._save(entries)

    def update_order_status(self, order_id: str, order_status: str) -> None:
        entries = self.load()
        for entry in entries:
            if entry.get("order_id") == order_id:
                entry["order_status"] = order_status
                if order_status in ("canceled", "rejected", "expired"):
                    entry["status"] = "void"
        self._save(entries)

    def record_tier(self, order_id: str, gain_pct: float, closed_qty: int) -> None:
        entries = self.load()
        for entry in entries:
            if entry.get("order_id") != order_id:
                continue
            fired = entry.setdefault("tiers_fired", [])
            if gain_pct not in fired:
                fired.append(gain_pct)
            remaining = int(entry.get("contracts_remaining", entry.get("contracts", 0)))
            entry["contracts_remaining"] = max(0, remaining - closed_qty)
            if entry["contracts_remaining"] == 0:
                entry["status"] = "closed"
                entry["closed_at"] = datetime.now(timezone.utc).isoformat()
        self._save(entries)


class Executor:
    def __init__(self, settings: Settings, rules: RiskRules):
        self.settings = settings
        self.rules = rules
        self.cli = AlpacaCLI(settings)
        self.journal = Journal(settings)

    def account_snapshot(self) -> Dict:
        return self.cli.account()

    def option_positions(self) -> List[Dict]:
        return [p for p in self.cli.positions() if p.get("asset_class") == "us_option"]

    def portfolio(self) -> PortfolioState:
        account = self.cli.account()
        positions = self.option_positions()
        underlyings = [self._underlying_of(p.get("symbol", "")) for p in positions]

        committed = 0.0
        for entry in self.journal.load():
            if entry.get("status") == "open":
                remaining = int(entry.get("contracts_remaining", entry.get("contracts", 0)))
                committed += float(entry.get("max_loss", 0)) * remaining

        cash = float(account.get("cash", 0) or 0)
        return PortfolioState(
            equity=float(account.get("equity", 0) or 0),
            cash=cash,
            options_buying_power=float(account.get("options_buying_power", cash) or cash),
            open_positions=len(set(underlyings)),
            open_underlyings=underlyings,
            committed_risk=committed,
        )

    def market_is_open(self) -> bool:
        try:
            return bool(self.cli.clock().get("is_open", False))
        except CLIError:
            return False

    def _guard_live(self) -> None:
        if not self.rules.is_live:
            raise ExecutionBlocked(
                "execution.mode is dry_run in risk_rules.json, refusing to place orders")

    def reconcile_orders(self) -> List[str]:
        notes = []
        for entry in self.journal.load():
            if entry.get("status") != "open":
                continue
            if entry.get("order_status") not in ("submitted", "new", "pending_new", "accepted"):
                continue
            try:
                order = self.cli.get_order(entry["order_id"])
            except CLIError:
                continue

            status = str(order.get("status", "")).lower()
            if not status or status == entry.get("order_status"):
                continue

            self.journal.update_order_status(entry["order_id"], status)
            if status in ("canceled", "rejected", "expired"):
                notes.append(f"{entry['underlying']} order {status}, journal voided")
            elif status == "filled":
                notes.append(f"{entry['underlying']} order filled")
        return notes

    def open_spread(self, spread: VerticalSpread, thesis: Thesis,
                    contracts: int) -> Optional[TradeRecord]:
        self._guard_live()

        legs = [
            AlpacaCLI.leg(spread.short_leg.symbol, "sell", "sell_to_open"),
            AlpacaCLI.leg(spread.long_leg.symbol, "buy", "buy_to_open"),
        ]
        limit_price = round(max(spread.credit * 0.90, 0.01), 2)

        try:
            order = self.cli.submit_mleg(legs, contracts, limit_price)
        except CLIError as exc:
            print(f"order rejected for {spread.underlying}: {exc}")
            return None

        record = TradeRecord(
            order_id=str(order.get("id", "")),
            underlying=spread.underlying,
            strategy="put_credit_spread" if spread.is_put else "call_credit_spread",
            short_symbol=spread.short_leg.symbol,
            long_symbol=spread.long_leg.symbol,
            contracts=contracts,
            contracts_remaining=contracts,
            credit=spread.credit,
            width=spread.width,
            max_loss=spread.max_loss,
            short_iv=round(spread.short_leg.iv, 4) if spread.short_leg.iv else None,
            opened_at=datetime.now(timezone.utc).isoformat(),
            sector=thesis.sector,
            direction=thesis.direction,
            conviction=round(thesis.conviction, 3),
            thesis=thesis.thesis,
            key_risk=thesis.key_risk,
            order_status=str(order.get("status", "submitted")).lower(),
        )
        self.journal.append(record.to_dict())
        return record

    def close_spread(self, entry: Dict, contracts: int) -> bool:
        self._guard_live()

        if contracts <= 0:
            return False

        legs = [
            AlpacaCLI.leg(entry["short_symbol"], "buy", "buy_to_close"),
            AlpacaCLI.leg(entry["long_symbol"], "sell", "sell_to_close"),
        ]
        limit_price = round(float(entry["width"]) * 0.95, 2)

        try:
            self.cli.submit_mleg(legs, contracts, limit_price)
        except CLIError as exc:
            print(f"close rejected for {entry['underlying']}: {exc}")
            return False

        remaining = int(entry.get("contracts_remaining", entry.get("contracts", 0)))
        if contracts >= remaining:
            self.journal.update_status(entry["order_id"], "closed")
        return True

    def preview_spread(self, spread: VerticalSpread, contracts: int) -> Dict:
        legs = [
            AlpacaCLI.leg(spread.short_leg.symbol, "sell", "sell_to_open"),
            AlpacaCLI.leg(spread.long_leg.symbol, "buy", "buy_to_open"),
        ]
        limit_price = round(max(spread.credit * 0.90, 0.01), 2)
        return self.cli.submit_mleg(legs, contracts, limit_price, dry_run=True)

    @staticmethod
    def _underlying_of(option_symbol: str) -> str:
        return option_symbol[:-15] if len(option_symbol) > 15 else option_symbol
