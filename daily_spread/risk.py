from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import Settings
from .market import VerticalSpread
from .signal import Thesis


@dataclass
class RiskDecision:
    approved: bool
    contracts: int
    reasons: List[str]

    @property
    def summary(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "ok"


@dataclass
class PortfolioState:
    equity: float
    cash: float
    options_buying_power: float
    open_positions: int
    open_underlyings: List[str]
    committed_risk: float


class RiskManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def screen_thesis(self, thesis: Thesis) -> Tuple[bool, List[str]]:
        reasons = []

        if not thesis.is_actionable:
            reasons.append(f"direction {thesis.direction} is not tradeable")
        if thesis.conviction < self.settings.min_conviction:
            reasons.append(
                f"conviction {thesis.conviction:.2f} below floor {self.settings.min_conviction:.2f}")
        if thesis.article_count < self.settings.min_articles_per_sector:
            reasons.append(f"only {thesis.article_count} articles")

        return (not reasons), reasons

    def screen_spread(self, spread: VerticalSpread, thesis: Thesis,
                      portfolio: PortfolioState) -> RiskDecision:
        reasons = []

        if portfolio.open_positions >= self.settings.max_open_positions:
            reasons.append(f"position cap {self.settings.max_open_positions} reached")

        if portfolio.open_underlyings.count(spread.underlying) >= self.settings.max_positions_per_underlying:
            reasons.append(f"already holding {spread.underlying}")

        if spread.dte < self.settings.target_dte_min or spread.dte > self.settings.target_dte_max:
            reasons.append(f"dte {spread.dte} outside window")

        if spread.credit_to_width < self.settings.min_credit_to_width:
            reasons.append(f"credit/width {spread.credit_to_width:.2f} too thin")

        if spread.max_loss <= 0:
            reasons.append("non positive max loss")
            return RiskDecision(False, 0, reasons)

        if spread.short_leg.spread_pct > 0.65 or spread.long_leg.spread_pct > 0.65:
            reasons.append("quoted bid ask too wide")

        risk_budget = portfolio.equity * self.settings.max_risk_per_trade_pct
        contracts = int(risk_budget // spread.max_loss)
        if contracts < 1:
            reasons.append(f"max loss {spread.max_loss:.0f} exceeds per trade budget {risk_budget:.0f}")
            contracts = 0

        total_cap = portfolio.equity * self.settings.max_total_risk_pct
        remaining = total_cap - portfolio.committed_risk
        if contracts > 0 and spread.max_loss * contracts > remaining:
            contracts = int(max(remaining, 0) // spread.max_loss)
            if contracts < 1:
                reasons.append("portfolio risk cap reached")
                contracts = 0

        if contracts > 0:
            required = spread.max_loss * contracts
            usable = portfolio.options_buying_power - portfolio.equity * self.settings.min_cash_buffer_pct
            if required > usable:
                contracts = int(max(usable, 0) // spread.max_loss)
                if contracts < 1:
                    reasons.append("cash buffer would be breached")
                    contracts = 0

        contracts = min(contracts, 10)
        approved = contracts >= 1 and not reasons
        return RiskDecision(approved, contracts, reasons)

    def select(self, thesis: Thesis, spreads: List[VerticalSpread],
               portfolio: PortfolioState) -> Tuple[Optional[VerticalSpread], RiskDecision]:
        last = RiskDecision(False, 0, ["no candidate spreads"])
        for spread in spreads:
            decision = self.screen_spread(spread, thesis, portfolio)
            if decision.approved:
                return spread, decision
            last = decision
        return None, last
