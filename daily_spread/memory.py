import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings


@dataclass
class Outcome:
    sector: str
    underlying: str
    direction: str
    conviction: float
    opened_at: str
    resolved_at: str
    result: str
    detail: str

    def to_dict(self) -> Dict:
        return self.__dict__.copy()

    def summary(self) -> str:
        opened = self.opened_at[:10]
        return f"{opened} {self.direction} conviction {self.conviction:.2f} -> {self.result}, {self.detail}"


class OutcomeLog:
    def __init__(self, settings: Settings):
        self.path = Path(settings.state_dir) / "outcomes.jsonl"

    def record(self, outcome: Outcome) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(outcome.to_dict()) + "\n")

    def load(self) -> List[Outcome]:
        if not self.path.exists():
            return []

        entries = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    entries.append(Outcome(**payload))
                except TypeError:
                    continue
        return entries

    def recent_for_sector(self, sector: str, limit: int = 5,
                          as_of: Optional[datetime] = None) -> List[Outcome]:
        entries = [e for e in self.load() if e.sector == sector]

        if as_of is not None:
            keep = []
            for entry in entries:
                try:
                    resolved = datetime.fromisoformat(entry.resolved_at)
                except ValueError:
                    continue
                if resolved.tzinfo is None:
                    resolved = resolved.replace(tzinfo=timezone.utc)
                if resolved <= as_of:
                    keep.append(entry)
            entries = keep

        entries.sort(key=lambda e: e.resolved_at)
        return entries[-limit:]


def format_memory(sector: str, outcomes: List[Outcome]) -> str:
    if not outcomes:
        return ""

    hits = sum(1 for o in outcomes if o.result == "correct")
    lines = [f"How your last {len(outcomes)} calls on {sector} actually turned out "
             f"({hits} of {len(outcomes)} correct):"]
    for outcome in outcomes:
        lines.append(f"- {outcome.summary()}")
    lines.append("Weigh this record when setting conviction. A direction that has repeatedly "
                 "been wrong on this sector deserves lower conviction or a neutral call.")
    return "\n".join(lines)


def outcome_from_trade(sector: str, underlying: str, direction: str, conviction: float,
                       opened_at: str, profit_pct: float, reason: str) -> Outcome:
    correct = profit_pct > 0
    return Outcome(
        sector=sector,
        underlying=underlying,
        direction=direction,
        conviction=conviction,
        opened_at=opened_at,
        resolved_at=datetime.now(timezone.utc).isoformat(),
        result="correct" if correct else "wrong",
        detail=f"{reason}, captured {profit_pct:+.0%} of credit",
    )
