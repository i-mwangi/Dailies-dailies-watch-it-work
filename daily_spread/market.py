from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import StockBarsRequest

from .config import Settings
from .pricing import delta as bs_delta
from .pricing import implied_vol


@dataclass
class Contract:
    symbol: str
    underlying: str
    strike: float
    expiration: date
    is_call: bool
    bid: float
    ask: float
    iv: Optional[float]
    delta: Optional[float]

    @property
    def mid(self) -> float:
        return round((self.bid + self.ask) / 2.0, 2)

    @property
    def spread_pct(self) -> float:
        if self.mid <= 0:
            return 1.0
        return (self.ask - self.bid) / self.mid

    @property
    def dte(self) -> int:
        return (self.expiration - datetime.now(timezone.utc).date()).days


@dataclass
class VerticalSpread:
    underlying: str
    short_leg: Contract
    long_leg: Contract
    is_put: bool
    credit: float
    width: float

    @property
    def max_loss(self) -> float:
        return round((self.width - self.credit) * 100.0, 2)

    @property
    def max_profit(self) -> float:
        return round(self.credit * 100.0, 2)

    @property
    def credit_to_width(self) -> float:
        if self.width <= 0:
            return 0.0
        return self.credit / self.width

    @property
    def dte(self) -> int:
        return self.short_leg.dte

    def describe(self) -> str:
        kind = "put credit spread" if self.is_put else "call credit spread"
        return (f"{self.underlying} {kind} {self.short_leg.strike:g}/{self.long_leg.strike:g} "
                f"exp {self.short_leg.expiration} dte {self.dte} credit {self.credit:.2f} "
                f"maxloss {self.max_loss:.0f}")


class MarketData:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stocks = StockHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)
        self.options = OptionHistoricalDataClient(settings.alpaca_key, settings.alpaca_secret)

    def spot(self, symbol: str) -> Optional[float]:
        try:
            res = self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
            return float(res[symbol].price)
        except Exception:
            return None

    def realized_vol(self, symbol: str, lookback: int = 30) -> Optional[float]:
        try:
            end = datetime.now(timezone.utc) - timedelta(minutes=20)
            start = end - timedelta(days=lookback * 2 + 10)
            bars = self.stocks.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end))
            closes = [b.close for b in bars[symbol]][-(lookback + 1):]
            if len(closes) < 10:
                return None
            rets = []
            for i in range(1, len(closes)):
                if closes[i - 1] > 0:
                    rets.append((closes[i] - closes[i - 1]) / closes[i - 1])
            if len(rets) < 5:
                return None
            mean = sum(rets) / len(rets)
            var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
            return (var ** 0.5) * (252 ** 0.5)
        except Exception:
            return None

    def chain(self, underlying: str, spot_price: float) -> List[Contract]:
        today = datetime.now(timezone.utc).date()
        try:
            raw = self.options.get_option_chain(OptionChainRequest(
                underlying_symbol=underlying,
                feed="indicative",
                expiration_date_gte=today + timedelta(days=self.settings.target_dte_min),
                expiration_date_lte=today + timedelta(days=self.settings.target_dte_max),
                strike_price_gte=spot_price * 0.80,
                strike_price_lte=spot_price * 1.20,
            ))
        except Exception:
            return []

        contracts = []
        for symbol, snap in raw.items():
            quote = getattr(snap, "latest_quote", None)
            if quote is None:
                continue
            bid = float(getattr(quote, "bid_price", 0) or 0)
            ask = float(getattr(quote, "ask_price", 0) or 0)
            if bid <= 0 or ask <= 0 or ask < bid:
                continue

            parsed = self._parse_symbol(symbol)
            if parsed is None:
                continue
            expiration, is_call, strike = parsed

            dte = (expiration - today).days
            if dte <= 0:
                continue

            t = dte / 365.0
            mid = (bid + ask) / 2.0
            iv = implied_vol(mid, spot_price, strike, t, is_call)
            d = bs_delta(spot_price, strike, t, iv, is_call) if iv else None

            contracts.append(Contract(
                symbol=symbol, underlying=underlying, strike=strike, expiration=expiration,
                is_call=is_call, bid=bid, ask=ask, iv=iv, delta=d))

        return contracts

    @staticmethod
    def _parse_symbol(symbol: str):
        try:
            body = symbol[-15:]
            root_len = len(symbol) - 15
            if root_len <= 0:
                return None
            yy, mm, dd = int(body[0:2]), int(body[2:4]), int(body[4:6])
            flag = body[6].upper()
            strike = int(body[7:15]) / 1000.0
            return date(2000 + yy, mm, dd), flag == "C", strike
        except Exception:
            return None


def build_vertical_spreads(contracts: List[Contract], settings: Settings,
                           is_put: bool) -> List[VerticalSpread]:
    pool = [c for c in contracts
            if c.is_call != is_put
            and c.delta is not None
            and c.iv is not None
            and c.mid > 0.05
            and c.spread_pct <= 0.65]

    by_expiry: Dict[date, List[Contract]] = {}
    for c in pool:
        by_expiry.setdefault(c.expiration, []).append(c)

    spreads = []
    target = settings.short_delta_target

    for expiration, group in by_expiry.items():
        group.sort(key=lambda c: c.strike)
        candidates = [c for c in group if 0.12 <= abs(c.delta) <= 0.45]
        if not candidates:
            continue

        short_leg = min(candidates, key=lambda c: abs(abs(c.delta) - target))

        for long_leg in group:
            if is_put and long_leg.strike >= short_leg.strike:
                continue
            if not is_put and long_leg.strike <= short_leg.strike:
                continue

            width = abs(short_leg.strike - long_leg.strike)
            if width < settings.spread_width_min or width > settings.spread_width_max:
                continue

            credit = round(short_leg.bid - long_leg.ask, 2)
            if credit <= 0:
                continue

            spread = VerticalSpread(
                underlying=short_leg.underlying, short_leg=short_leg, long_leg=long_leg,
                is_put=is_put, credit=credit, width=width)

            if spread.credit_to_width < settings.min_credit_to_width:
                continue
            if spread.credit_to_width > 0.90:
                continue

            spreads.append(spread)

    spreads.sort(key=lambda s: s.credit_to_width, reverse=True)
    return spreads
