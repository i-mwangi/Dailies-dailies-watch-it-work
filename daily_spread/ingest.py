import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest

from .config import SECTOR_UNIVERSE, Settings

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    if not text:
        return ""
    return _TAG_RE.sub(" ", text).replace("&nbsp;", " ").strip()


@dataclass
class Article:
    id: int
    created_at: datetime
    headline: str
    summary: str
    source: str
    symbols: List[str]
    url: str

    def digest(self, limit: int = 320) -> str:
        body = f"{self.headline}. {self.summary}".strip()
        body = re.sub(r"\s+", " ", body)
        return body[:limit]


class NewsFeed:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = NewsClient(settings.alpaca_key, settings.alpaca_secret)

    def fetch(self) -> List[Article]:
        start = datetime.now(timezone.utc) - timedelta(hours=self.settings.news_lookback_hours)
        request = NewsRequest(
            start=start,
            limit=self.settings.news_limit,
            sort="desc",
            include_content=False,
            exclude_contentless=True,
        )
        payload = self.client.get_news(request)
        raw = payload.data.get("news", []) if hasattr(payload, "data") else []

        articles = []
        for item in raw:
            articles.append(
                Article(
                    id=int(getattr(item, "id", 0) or 0),
                    created_at=item.created_at,
                    headline=_clean(getattr(item, "headline", "")),
                    summary=_clean(getattr(item, "summary", "")),
                    source=getattr(item, "source", "") or "unknown",
                    symbols=[s.upper() for s in (getattr(item, "symbols", []) or [])],
                    url=getattr(item, "url", "") or "",
                )
            )
        return articles


def bucket_by_sector(articles: List[Article]) -> Dict[str, List[Article]]:
    buckets: Dict[str, List[Article]] = {name: [] for name in SECTOR_UNIVERSE}

    for article in articles:
        text = f"{article.headline} {article.summary}".lower()
        symbols = set(article.symbols)
        matched = set()

        for sector, spec in SECTOR_UNIVERSE.items():
            proxies = set(str(p).upper() for p in spec["proxies"])
            if symbols & proxies:
                matched.add(sector)
                continue
            for keyword in spec["keywords"]:
                if keyword in text:
                    matched.add(sector)
                    break

        for sector in matched:
            buckets[sector].append(article)

    return {sector: items for sector, items in buckets.items() if items}
