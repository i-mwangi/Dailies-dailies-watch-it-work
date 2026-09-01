import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"missing required environment variable: {name}")
    return value


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


SECTOR_UNIVERSE: Dict[str, Dict[str, object]] = {
    "Energy": {
        "etf": "XLE",
        "proxies": ["XOM", "CVX", "COP", "SLB", "OXY", "PSX", "MPC", "VLO", "EOG"],
        "keywords": ["oil", "crude", "natural gas", "opec", "refinery", "energy policy", "barrel", "lng"],
    },
    "Inflation": {
        "etf": "TLT",
        "proxies": ["TLT", "IEF", "SHY", "TIP"],
        "keywords": ["inflation", "cpi", "pce", "consumer price", "federal reserve", "rate cut", "rate hike", "fomc"],
    },
    "Stocks": {
        "etf": "SPY",
        "proxies": ["SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"],
        "keywords": ["equities", "s&p 500", "nasdaq", "earnings", "guidance", "wall street", "stock market"],
    },
    "Bonds": {
        "etf": "TLT",
        "proxies": ["TLT", "IEF", "LQD", "HYG", "AGG"],
        "keywords": ["treasury", "yield", "bond", "fixed income", "credit spread", "auction", "duration"],
    },
    "Commodities": {
        "etf": "GLD",
        "proxies": ["GLD", "SLV", "USO", "DBA", "FCX", "NEM"],
        "keywords": ["gold", "silver", "copper", "commodity", "metals", "wheat", "futures"],
    },
    "Financials": {
        "etf": "XLF",
        "proxies": ["JPM", "BAC", "GS", "MS", "WFC", "C", "SCHW"],
        "keywords": ["bank", "lending", "deposits", "net interest margin", "regulator", "basel"],
    },
    "RealEstate": {
        "etf": "XLRE",
        "proxies": ["XLRE", "VNQ", "SPG", "PLD", "AMT", "O"],
        "keywords": ["real estate", "housing", "mortgage", "reit", "commercial property", "home prices"],
    },
    "Infrastructure": {
        "etf": "XLI",
        "proxies": ["CAT", "DE", "HON", "UNP", "GE", "URI", "PWR"],
        "keywords": ["infrastructure", "construction", "public works", "freight", "industrial production"],
    },
    "Technology": {
        "etf": "XLK",
        "proxies": ["NVDA", "MSFT", "AAPL", "AVGO", "AMD", "INTC", "SMCI"],
        "keywords": ["semiconductor", "chip", "artificial intelligence", "data center", "cloud", "software"],
    },
}

TRADABLE_UNDERLYINGS: List[str] = ["XLE", "TLT", "SPY", "GLD", "XLF", "XLRE", "XLI", "XLK"]


@dataclass
class Settings:
    alpaca_key: str
    alpaca_secret: str
    paper: bool
    featherless_key: str
    featherless_base: str
    model: str

    news_lookback_hours: int = 18
    news_limit: int = 120
    min_articles_per_sector: int = 2

    min_conviction: float = 0.62
    max_open_positions: int = 5
    max_positions_per_underlying: int = 1
    max_risk_per_trade_pct: float = 0.02
    max_total_risk_pct: float = 0.10
    min_cash_buffer_pct: float = 0.25

    target_dte_min: int = 7
    target_dte_max: int = 45
    short_delta_target: float = 0.30
    spread_width_min: float = 1.0
    spread_width_max: float = 10.0
    min_credit_to_width: float = 0.20
    min_open_interest: int = 25

    profit_take_pct: float = 0.55
    stop_loss_multiple: float = 2.0
    close_at_dte: int = 2

    aggression: str = "moderate"

    state_dir: Path = field(default_factory=lambda: ROOT / "state")
    log_dir: Path = field(default_factory=lambda: ROOT / "logs")

    def apply_aggression(self) -> "Settings":
        if self.aggression == "conservative":
            self.min_conviction = 0.72
            self.max_open_positions = 3
            self.max_risk_per_trade_pct = 0.01
            self.max_total_risk_pct = 0.05
        elif self.aggression == "aggressive":
            self.min_conviction = 0.55
            self.max_open_positions = 8
            self.max_risk_per_trade_pct = 0.035
            self.max_total_risk_pct = 0.20
            self.min_cash_buffer_pct = 0.15
        return self


def load_settings() -> Settings:
    settings = Settings(
        alpaca_key=_require("ALPACA_API_KEY"),
        alpaca_secret=_require("ALPACA_SECRET_KEY"),
        paper=_flag("ALPACA_PAPER", True),
        featherless_key=_require("FEATHERLESS_API_KEY"),
        featherless_base=os.getenv("FEATHERLESS_API_BASE_URL", "https://api.featherless.ai/v1").strip().rstrip("/"),
        model=os.getenv("FEATHERLESS_MODEL", "Qwen/Qwen2.5-72B-Instruct").strip(),
        aggression=os.getenv("AGGRESSION", "moderate").strip().lower(),
    )
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    return settings.apply_aggression()
