import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings


class AuditLog:
    def __init__(self, settings: Settings):
        self.path = Path(settings.state_dir) / "trade_log.jsonl"

    def write(self, stage: str, **fields) -> Dict:
        now = datetime.now(timezone.utc)
        record = {
            "date": now.strftime("%Y-%m-%d"),
            "timestamp": now.strftime("%H:%M:%S"),
            "stage": stage,
        }
        record.update(fields)

        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record

    def read(self, stage: Optional[str] = None) -> List[Dict]:
        if not self.path.exists():
            return []

        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if stage is None or record.get("stage") == stage:
                    records.append(record)
        return records

    def cycle_count(self) -> int:
        return len(self.read(stage="cycle"))

    def screened(self, sector: str, underlying: str, article_count: int) -> None:
        self.write("screened", sector=sector, underlying=underlying,
                   article_count=article_count)

    def thesis(self, payload: Dict) -> None:
        self.write("thesis", **payload)

    def risk_check(self, sector: str, underlying: str, passed: bool,
                   reason: str, **extra) -> None:
        self.write("risk_check", sector=sector, underlying=underlying,
                   passed=passed, reason=reason, **extra)

    def order(self, mode: str, action: str, underlying: str, **extra) -> None:
        self.write("order", mode=mode, action=action, underlying=underlying, **extra)

    def cycle(self, **fields) -> None:
        self.write("cycle", **fields)
