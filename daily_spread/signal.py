import json
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from .config import SECTOR_UNIVERSE, Settings
from .ingest import Article
from .llm import FeatherlessClient, LLMError, LLMUnavailable

SYSTEM_PROMPT = """You are a macro strategist who converts financial news into directional options theses.

You read a batch of headlines for one market sector and judge where that sector trades over the next two to six weeks.

Rules:
- Judge the sector, not individual companies.
- Distinguish signal from noise. Routine analyst notes, price-target tweaks and single-stock earnings chatter are weak evidence about a whole sector.
- Conviction must reflect evidence strength. Thin or conflicting news means conviction below 0.5.
- Never claim certainty. There is no such thing as a sure trade.
- Return only a JSON object, no prose outside it.

Output schema:
{
  "direction": "bullish" | "bearish" | "neutral",
  "conviction": 0.0 to 1.0,
  "horizon_days": integer 7 to 45,
  "thesis": "two or three sentences of reasoning grounded in the headlines",
  "key_risk": "the single most likely way this thesis is wrong",
  "evidence": ["headline fragments that drove the call"]
}"""


@dataclass
class Thesis:
    sector: str
    underlying: str
    direction: str
    conviction: float
    horizon_days: int
    thesis: str
    key_risk: str
    evidence: List[str]
    article_count: int

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("bullish", "bearish")

    def to_dict(self) -> Dict:
        return {
            "sector": self.sector,
            "underlying": self.underlying,
            "direction": self.direction,
            "conviction": round(self.conviction, 3),
            "horizon_days": self.horizon_days,
            "thesis": self.thesis,
            "key_risk": self.key_risk,
            "evidence": self.evidence,
            "article_count": self.article_count,
        }


class ThesisEngine:
    def __init__(self, settings: Settings, llm: Optional[FeatherlessClient] = None):
        self.settings = settings
        self.llm = llm or FeatherlessClient(settings)

    def analyse(self, sector: str, articles: List[Article],
                memory_note: str = "") -> Optional[Thesis]:
        if len(articles) < self.settings.min_articles_per_sector:
            return None

        spec = SECTOR_UNIVERSE.get(sector)
        if not spec:
            return None

        ordered = sorted(articles, key=lambda a: a.created_at, reverse=True)[:18]
        lines = []
        for article in ordered:
            stamp = article.created_at.strftime("%m-%d %H:%M")
            tags = ",".join(article.symbols[:4]) or "-"
            lines.append(f"[{stamp}] ({tags}) {article.digest()}")

        user_prompt = (
            f"Sector: {sector}\n"
            f"Traded via: {spec['etf']}\n"
            f"Headlines from the last {self.settings.news_lookback_hours} hours:\n\n"
            + "\n".join(lines)
        )

        if memory_note:
            user_prompt += f"\n\n{memory_note}"

        user_prompt += "\n\nReturn the JSON object."

        try:
            payload = self.llm.complete_json(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content": user_prompt}],
                max_tokens=650)
        except LLMUnavailable:
            raise
        except LLMError:
            return None

        if not payload:
            return None

        direction = str(payload.get("direction", "neutral")).strip().lower()
        if direction not in ("bullish", "bearish", "neutral"):
            direction = "neutral"

        try:
            conviction = float(payload.get("conviction", 0.0))
        except (TypeError, ValueError):
            conviction = 0.0
        conviction = max(0.0, min(1.0, conviction))

        try:
            horizon = int(payload.get("horizon_days", 21))
        except (TypeError, ValueError):
            horizon = 21
        horizon = max(7, min(45, horizon))

        evidence = payload.get("evidence", [])
        if not isinstance(evidence, list):
            evidence = []

        return Thesis(
            sector=sector,
            underlying=str(spec["etf"]),
            direction=direction,
            conviction=conviction,
            horizon_days=horizon,
            thesis=str(payload.get("thesis", "")).strip(),
            key_risk=str(payload.get("key_risk", "")).strip(),
            evidence=[str(e) for e in evidence[:5]],
            article_count=len(articles),
        )

    def analyse_all(self, buckets: Dict[str, List[Article]],
                    memory_notes: Optional[Dict[str, str]] = None,
                    deadline: Optional[float] = None) -> List[Thesis]:
        notes = memory_notes or {}
        results = []
        for sector, articles in buckets.items():
            if deadline is not None and time.monotonic() > deadline:
                break
            thesis = self.analyse(sector, articles, notes.get(sector, ""))
            if thesis:
                results.append(thesis)
        results.sort(key=lambda t: t.conviction, reverse=True)
        return results

    def eligible_sectors(self, buckets: Dict[str, List[Article]]) -> Dict[str, List[Article]]:
        return {sector: articles for sector, articles in buckets.items()
                if len(articles) >= self.settings.min_articles_per_sector
                and sector in SECTOR_UNIVERSE}
