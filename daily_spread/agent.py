import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .audit import AuditLog
from .config import Settings, load_settings
from .execute import ExecutionBlocked, Executor
from .guards import DrawdownGuard, LossLimitGuard, ReentryLock, StopLossGuard
from .ingest import NewsFeed, bucket_by_sector
from .llm import FeatherlessClient, LLMUnavailable
from .market import MarketData, build_vertical_spreads
from .memory import OutcomeLog, format_memory
from .monitor import PositionMonitor
from .risk import PortfolioState, RiskManager
from .rules import RiskRules, apply_rules, load_rules
from .signal import ThesisEngine


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


class TradingAgent:
    def __init__(self, settings: Settings, rules: RiskRules):
        self.settings = settings
        self.rules = rules
        self.audit = AuditLog(settings)
        self.llm = FeatherlessClient(settings)
        self.feed = NewsFeed(settings)
        self.engine = ThesisEngine(settings, self.llm)
        self.market = MarketData(settings)
        self.executor = Executor(settings, rules)
        self.risk = RiskManager(settings, rules.allowed_directions)
        self.lock = ReentryLock(settings, rules)
        self.loss_guard = LossLimitGuard(settings, rules)
        self.stop_guard = StopLossGuard(settings, rules, self.audit)
        self.drawdown_guard = DrawdownGuard(settings, rules)
        self.outcomes = OutcomeLog(settings)
        self.monitor = PositionMonitor(settings, rules, self.executor,
                                       self.market, self.audit, self.lock,
                                       self.outcomes)

    def run_cycle(self, dry_run: bool = False) -> Dict:
        started = datetime.now(timezone.utc)
        deadline = time.monotonic() + self.rules.cycle_budget_seconds
        simulate = dry_run or not self.rules.is_live

        log(f"cycle start mode={self.rules.mode} simulate={simulate} "
            f"aggression={self.settings.aggression}")

        summary = {
            "started_at": started.isoformat(),
            "mode": self.rules.mode,
            "simulate": simulate,
            "market_open": self.executor.market_is_open(),
            "theses": [],
            "actions": [],
            "trades": [],
        }

        account = self.executor.account_snapshot()
        equity = float(account.get("equity", 0) or 0)
        last_equity = float(account.get("last_equity", equity) or equity)

        loss_status = self.loss_guard.evaluate(equity, last_equity)
        summary["loss_limits"] = loss_status.to_dict()
        log(f"equity={equity:.2f} daily_pnl={loss_status.daily_pnl:+.2f} "
            f"weekly_pnl={loss_status.weekly_pnl:+.2f} -> {loss_status.reason}")

        for note in self.executor.reconcile_orders():
            log(f"  {note}")
            summary["actions"].append(note)

        log("reviewing open positions")
        for action in self.monitor.review():
            log(f"  {action}")
            summary["actions"].append(action)

        protections = [self.stop_guard.evaluate(), self.drawdown_guard.evaluate(equity)]
        summary["protections"] = [p.to_dict() for p in protections]
        for protection in protections:
            marker = "LOCKED" if protection.locked else "ok"
            log(f"protection {protection.source}: {marker} - {protection.reason}")

        active = [p for p in protections if p.locked]

        if loss_status.entries_halted or active:
            if loss_status.entries_halted:
                reason = f"loss limit: {loss_status.reason}"
            else:
                reason = f"{active[0].source}: {active[0].reason}"
            log(f"entries halted, exits still active - {reason}")
            summary["actions"].append(f"entries halted: {reason}")
            self.audit.write("entries_halted", reason=reason)
            return self._finish(summary)

        articles = self.feed.fetch()
        log(f"pulled {len(articles)} articles")
        buckets = bucket_by_sector(articles)
        coverage = ", ".join(f"{name}:{len(items)}" for name, items in buckets.items())
        log(f"sectors: {coverage}")

        for sector, items in buckets.items():
            self.audit.screened(sector, "", len(items))

        eligible = self.engine.eligible_sectors(buckets)
        skipped = sorted(set(buckets) - set(eligible))
        if skipped:
            log(f"below {self.settings.min_articles_per_sector} article floor, "
                f"no model call: {', '.join(skipped)}")

        memory_notes = {}
        for sector in eligible:
            recent = self.outcomes.recent_for_sector(sector, limit=5)
            note = format_memory(sector, recent)
            if note:
                memory_notes[sector] = note
        if memory_notes:
            log(f"replaying outcomes for: {', '.join(sorted(memory_notes))}")

        try:
            theses = self.engine.analyse_all(eligible, memory_notes, deadline)
        except LLMUnavailable as exc:
            message = f"model unavailable, no theses this cycle: {exc}"
            log(message)
            summary["actions"].append(message)
            summary["llm_unavailable"] = True
            self.audit.write("llm_unavailable", detail=str(exc)[:300])
            return self._finish(summary)

        if len(theses) < len(eligible):
            log(f"time budget reached, analysed {len(theses)} of {len(eligible)} sectors")
            summary["actions"].append(
                f"time budget reached after {len(theses)} of {len(eligible)} sectors")

        for thesis in theses:
            log(f"  {thesis.sector:14s} {thesis.direction:8s} conviction={thesis.conviction:.2f}")
            summary["theses"].append(thesis.to_dict())
            self.audit.thesis(thesis.to_dict())

        portfolio = self.executor.portfolio()
        log(f"open={portfolio.open_positions} committed_risk={portfolio.committed_risk:.2f}")

        locked = self.lock.active()
        if locked:
            log(f"re-entry locked: {', '.join(locked)}")

        if not summary["market_open"] and not simulate:
            log("market closed, skipping entries")
            summary["actions"].append("market closed")
            return self._finish(summary)

        claimed: List[str] = []
        pending_risk = 0.0

        for thesis in theses:
            passed, reasons = self.risk.screen_thesis(thesis)
            if not passed:
                reason = "; ".join(reasons)
                log(f"  skip {thesis.sector}: {reason}")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, reason)
                continue

            remaining_lock = self.lock.remaining_hours(thesis.underlying)
            if remaining_lock is not None:
                reason = f"re-entry locked for {remaining_lock:.1f}h"
                log(f"  skip {thesis.sector}: {reason}")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, reason)
                continue

            if thesis.underlying in claimed:
                reason = f"{thesis.underlying} already claimed this cycle"
                log(f"  skip {thesis.sector}: {reason}")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, reason)
                continue

            spot = self.market.spot(thesis.underlying)
            if spot is None:
                log(f"  skip {thesis.sector}: no spot for {thesis.underlying}")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, "no spot price")
                continue

            contracts = self.market.chain(thesis.underlying, spot)
            if not contracts:
                log(f"  skip {thesis.sector}: empty chain")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, "empty chain")
                continue

            is_put = thesis.direction == "bullish"
            spreads = build_vertical_spreads(contracts, self.settings, is_put=is_put)
            if not spreads:
                log(f"  skip {thesis.sector}: no spread cleared construction filters")
                self.audit.risk_check(thesis.sector, thesis.underlying, False,
                                      "no spread cleared construction filters")
                continue

            probe = PortfolioState(
                equity=portfolio.equity,
                cash=portfolio.cash,
                options_buying_power=portfolio.options_buying_power,
                open_positions=portfolio.open_positions + len(claimed),
                open_underlyings=portfolio.open_underlyings + claimed,
                committed_risk=portfolio.committed_risk + pending_risk,
            )
            spread, decision = self.risk.select(thesis, spreads, probe)

            if spread is None or not decision.approved:
                log(f"  skip {thesis.sector}: {decision.summary}")
                self.audit.risk_check(thesis.sector, thesis.underlying, False, decision.summary)
                continue

            size = min(decision.contracts, self.rules.max_contracts_per_spread)
            log(f"  candidate {spread.describe()} x{size}")
            self.audit.risk_check(thesis.sector, thesis.underlying, True, "approved",
                                  contracts=size, max_loss=spread.max_loss,
                                  credit=spread.credit, dte=spread.dte,
                                  conviction=round(thesis.conviction, 3))

            if simulate:
                self.audit.order("dry_run", "open", thesis.underlying,
                                 spread=spread.describe(), contracts=size,
                                 would_execute=True)
                summary["trades"].append({"simulated": True, "spread": spread.describe(),
                                          "contracts": size, "sector": thesis.sector})
                claimed.append(thesis.underlying)
                pending_risk += spread.max_loss * size
                continue

            try:
                record = self.executor.open_spread(spread, thesis, size)
            except ExecutionBlocked as exc:
                log(f"  blocked: {exc}")
                summary["actions"].append(str(exc))
                break

            if record:
                log(f"  submitted {record.order_id} {record.strategy} x{record.contracts}")
                self.audit.order("live", "open", thesis.underlying,
                                 order_id=record.order_id, contracts=record.contracts,
                                 credit=record.credit, max_loss=record.max_loss,
                                 placed=True)
                summary["trades"].append(record.to_dict())
                claimed.append(thesis.underlying)
                pending_risk += spread.max_loss * size
                portfolio = self.executor.portfolio()

        return self._finish(summary)

    def _finish(self, summary: Dict) -> Dict:
        summary["finished_at"] = datetime.now(timezone.utc).isoformat()
        summary["llm_usage"] = self.llm.usage()

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = Path(self.settings.log_dir) / f"cycle_{stamp}.json"
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        self.audit.cycle(mode=summary["mode"], simulate=summary["simulate"],
                         theses=len(summary["theses"]), trades=len(summary["trades"]),
                         market_open=summary["market_open"],
                         llm_calls=summary["llm_usage"]["calls"],
                         llm_tokens=summary["llm_usage"]["total_tokens"])

        usage = summary["llm_usage"]
        log(f"cycle complete, {len(summary['trades'])} trade actions, "
            f"{usage['calls']} model calls, {usage['total_tokens']} tokens, log {path.name}")
        return summary

    def run_forever(self, interval_minutes: int) -> None:
        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                log("interrupted")
                break
            except Exception as exc:
                log(f"cycle error: {type(exc).__name__}: {str(exc)[:200]}")
            log(f"sleeping {interval_minutes} minutes")
            time.sleep(interval_minutes * 60)


def build_agent() -> TradingAgent:
    settings = load_settings()
    rules = load_rules()
    apply_rules(settings, rules)
    return TradingAgent(settings, rules)
