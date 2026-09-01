import json
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from .config import SECTOR_UNIVERSE, Settings, TRADABLE_UNDERLYINGS
from .ingest import Article, NewsFeed, bucket_by_sector
from .llm import LLMUnavailable
from .signal import Thesis, ThesisEngine


@dataclass
class Observation:
    as_of: str
    sector: str
    underlying: str
    direction: str
    conviction: float
    article_count: int
    entry_price: float
    exit_price: float
    forward_return: float
    horizon_days: int

    @property
    def is_hit(self) -> bool:
        if self.direction == "bullish":
            return self.forward_return > 0
        if self.direction == "bearish":
            return self.forward_return < 0
        return False

    @property
    def signed_return(self) -> float:
        if self.direction == "bullish":
            return self.forward_return
        if self.direction == "bearish":
            return -self.forward_return
        return 0.0

    def to_dict(self) -> Dict:
        payload = self.__dict__.copy()
        payload["is_hit"] = self.is_hit
        payload["signed_return"] = round(self.signed_return, 6)
        return payload


@dataclass
class BacktestReport:
    horizon_days: int
    observations: List[Observation] = field(default_factory=list)
    skipped: Dict[str, int] = field(default_factory=dict)

    def directional(self) -> List[Observation]:
        return [o for o in self.observations if o.direction in ("bullish", "bearish")]

    def summarise(self, minimum_conviction: float = 0.0) -> Dict:
        sample = [o for o in self.directional() if o.conviction >= minimum_conviction]
        if not sample:
            return {"count": 0}

        hits = [o for o in sample if o.is_hit]
        signed = [o.signed_return for o in sample]
        raw = [o.forward_return for o in sample]

        wins = [s for s in signed if s > 0]
        losses = [s for s in signed if s < 0]

        gross_win = sum(wins)
        gross_loss = abs(sum(losses))

        return {
            "count": len(sample),
            "hit_rate": round(len(hits) / len(sample), 4),
            "mean_signed_return": round(statistics.fmean(signed), 6),
            "median_signed_return": round(statistics.median(signed), 6),
            "mean_underlying_return": round(statistics.fmean(raw), 6),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "stdev_signed_return": round(statistics.stdev(signed), 6) if len(signed) > 1 else None,
            "best": round(max(signed), 6),
            "worst": round(min(signed), 6),
        }

    def baseline(self) -> Dict:
        sample = self.directional()
        if not sample:
            return {"count": 0}

        raw = [o.forward_return for o in sample]
        always_long_hits = [r for r in raw if r > 0]
        return {
            "count": len(sample),
            "always_long_hit_rate": round(len(always_long_hits) / len(raw), 4),
            "mean_underlying_return": round(statistics.fmean(raw), 6),
        }

    def by_conviction(self) -> Dict:
        buckets = {}
        edges = [(0.55, 0.60), (0.60, 0.70), (0.70, 1.01)]
        for low, high in edges:
            sample = [o for o in self.directional() if low <= o.conviction < high]
            if not sample:
                continue
            hits = [o for o in sample if o.is_hit]
            buckets[f"{low:.2f}-{high:.2f}"] = {
                "count": len(sample),
                "hit_rate": round(len(hits) / len(sample), 4),
                "mean_signed_return": round(
                    statistics.fmean([o.signed_return for o in sample]), 6),
            }
        return buckets

    def by_sector(self) -> Dict:
        buckets = {}
        for sector in sorted({o.sector for o in self.directional()}):
            sample = [o for o in self.directional() if o.sector == sector]
            hits = [o for o in sample if o.is_hit]
            buckets[sector] = {
                "count": len(sample),
                "hit_rate": round(len(hits) / len(sample), 4),
                "mean_signed_return": round(
                    statistics.fmean([o.signed_return for o in sample]), 6),
            }
        return buckets

    def by_direction(self) -> Dict:
        buckets = {}
        for direction in ("bullish", "bearish"):
            sample = [o for o in self.directional() if o.direction == direction]
            if not sample:
                continue
            hits = [o for o in sample if o.is_hit]
            buckets[direction] = {
                "count": len(sample),
                "hit_rate": round(len(hits) / len(sample), 4),
                "mean_signed_return": round(
                    statistics.fmean([o.signed_return for o in sample]), 6),
            }
        return buckets

    def to_dict(self, minimum_conviction: float) -> Dict:
        return {
            "horizon_days": self.horizon_days,
            "min_conviction": minimum_conviction,
            "overall": self.summarise(minimum_conviction),
            "all_directional": self.summarise(0.0),
            "baseline_always_long": self.baseline(),
            "by_conviction": self.by_conviction(),
            "by_direction": self.by_direction(),
            "by_sector": self.by_sector(),
            "skipped": self.skipped,
            "observations": [o.to_dict() for o in self.observations],
        }


class ThesisCache:
    def __init__(self, settings: Settings):
        self.path = Path(settings.state_dir) / "backtest_cache.json"
        self._data = self._load()

    def _load(self) -> Dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def key(self, as_of: date, sector: str, model: str) -> str:
        return f"{as_of.isoformat()}|{sector}|{model}"

    def get(self, key: str) -> Optional[Dict]:
        return self._data.get(key)

    def put(self, key: str, payload: Dict) -> None:
        self._data[key] = payload
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    def size(self) -> int:
        return len(self._data)


class SignalBacktest:
    def __init__(self, settings: Settings, engine: ThesisEngine):
        self.settings = settings
        self.engine = engine
        self.feed = NewsFeed(settings)
        self.stocks = StockHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)
        self.cache = ThesisCache(settings)

    def fetch_news_window(self, start: datetime, end: datetime, limit: int = 200) -> List[Article]:
        from alpaca.data.requests import NewsRequest
        from .ingest import _clean

        request = NewsRequest(start=start, end=end, limit=limit, sort="desc",
                              include_content=False, exclude_contentless=True)
        payload = self.feed.client.get_news(request)
        raw = payload.data.get("news", []) if hasattr(payload, "data") else []

        articles = []
        for item in raw:
            articles.append(Article(
                id=int(getattr(item, "id", 0) or 0),
                created_at=item.created_at,
                headline=_clean(getattr(item, "headline", "")),
                summary=_clean(getattr(item, "summary", "")),
                source=getattr(item, "source", "") or "unknown",
                symbols=[s.upper() for s in (getattr(item, "symbols", []) or [])],
                url=getattr(item, "url", "") or "",
            ))
        return articles

    def load_bars(self, symbols: List[str], start: datetime, end: datetime) -> Dict[str, Dict[date, float]]:
        latest = datetime.now(timezone.utc) - timedelta(minutes=20)
        if end > latest:
            end = latest
        try:
            bars = self.stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame.Day, start=start, end=end))
        except Exception:
            return {}

        series: Dict[str, Dict[date, float]] = {}
        for symbol in symbols:
            try:
                series[symbol] = {b.timestamp.date(): float(b.close) for b in bars[symbol]}
            except KeyError:
                series[symbol] = {}
        return series

    @staticmethod
    def forward_pair(prices: Dict[date, float], as_of: date,
                     horizon: int) -> Optional[tuple]:
        trading_days = sorted(d for d in prices if d >= as_of)
        if len(trading_days) <= horizon:
            return None
        entry_day = trading_days[0]
        exit_day = trading_days[horizon]
        entry = prices[entry_day]
        exit_price = prices[exit_day]
        if entry <= 0:
            return None
        return entry, exit_price, (exit_price - entry) / entry

    def run(self, days: int, horizon: int, step_days: int = 1,
            progress=None) -> BacktestReport:
        report = BacktestReport(horizon_days=horizon)
        skipped: Dict[str, int] = {}

        now = datetime.now(timezone.utc)
        window_end = now - timedelta(days=horizon + 2)
        window_start = window_end - timedelta(days=days)

        bar_start = window_start - timedelta(days=10)
        prices = self.load_bars(TRADABLE_UNDERLYINGS, bar_start, now)

        cursor = window_start
        while cursor <= window_end:
            as_of = cursor.date()
            slice_end = cursor
            slice_start = cursor - timedelta(hours=self.settings.news_lookback_hours)

            try:
                articles = self.fetch_news_window(slice_start, slice_end)
            except Exception:
                skipped["news_fetch_failed"] = skipped.get("news_fetch_failed", 0) + 1
                cursor += timedelta(days=step_days)
                continue

            buckets = bucket_by_sector(articles)
            eligible = self.engine.eligible_sectors(buckets)

            for sector, items in eligible.items():
                underlying = str(SECTOR_UNIVERSE[sector]["etf"])
                series = prices.get(underlying, {})
                pair = self.forward_pair(series, as_of, horizon)
                if pair is None:
                    skipped["no_forward_price"] = skipped.get("no_forward_price", 0) + 1
                    continue

                key = self.cache.key(as_of, sector, self.settings.model)
                cached = self.cache.get(key)

                if cached is None:
                    try:
                        thesis = self.engine.analyse(sector, items)
                    except LLMUnavailable:
                        raise
                    if thesis is None:
                        skipped["thesis_failed"] = skipped.get("thesis_failed", 0) + 1
                        continue
                    cached = thesis.to_dict()
                    self.cache.put(key, cached)

                entry, exit_price, forward = pair
                report.observations.append(Observation(
                    as_of=as_of.isoformat(),
                    sector=sector,
                    underlying=underlying,
                    direction=cached["direction"],
                    conviction=float(cached["conviction"]),
                    article_count=int(cached.get("article_count", len(items))),
                    entry_price=round(entry, 4),
                    exit_price=round(exit_price, 4),
                    forward_return=round(forward, 6),
                    horizon_days=horizon,
                ))

            if progress:
                progress(as_of, len(report.observations))
            cursor += timedelta(days=step_days)

        report.skipped = skipped
        return report
