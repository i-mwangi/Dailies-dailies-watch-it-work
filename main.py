import argparse
import json

from daily_spread.agent import build_agent, log
from daily_spread.rules import load_rules


def main() -> None:
    parser = argparse.ArgumentParser(prog="daily-spread")
    parser.add_argument("command", choices=["once", "loop", "status", "monitor", "rules", "backtest"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--interval", type=int, default=0)
    parser.add_argument("--aggression", choices=["conservative", "moderate", "aggressive"])
    parser.add_argument("--min-conviction", type=float)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--step", type=int, default=1)
    parser.add_argument("--memory", action="store_true")
    args = parser.parse_args()

    if args.command == "rules":
        rules = load_rules()
        print(json.dumps({
            "mode": rules.mode,
            "is_live": rules.is_live,
            "account_number": rules.account_number,
            "starting_capital": rules.starting_capital,
            "daily_loss_cap": rules.daily_loss_cap(),
            "weekly_loss_cap": rules.weekly_loss_cap(),
            "stop_mode": rules.stop_mode,
            "tiers": [{"gain_pct": t.gain_pct, "close_fraction": t.close_fraction}
                      for t in rules.tiers],
            "reentry_enabled": rules.reentry_enabled,
        }, indent=2))
        return

    agent = build_agent()

    if args.aggression:
        agent.settings.aggression = args.aggression
        agent.settings.apply_aggression()
    if args.min_conviction is not None:
        agent.settings.min_conviction = args.min_conviction

    if args.command == "backtest":
        from daily_spread.backtest import SignalBacktest

        runner = SignalBacktest(agent.settings, agent.engine)
        log(f"replaying {args.days} days, horizon {args.horizon} trading days, "
            f"step {args.step}d, cache holds {runner.cache.size()} theses")

        def progress(as_of, count):
            log(f"  {as_of} observations={count}")

        report = runner.run(args.days, args.horizon, args.step, progress=progress,
                            use_memory=args.memory)
        payload = report.to_dict(agent.settings.min_conviction)

        suffix = "_mem" if args.memory else ""
        out = agent.settings.log_dir / f"backtest_{args.days}d_h{args.horizon}{suffix}.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        print(json.dumps({k: v for k, v in payload.items() if k != "observations"}, indent=2))
        log(f"model calls this run: {agent.llm.usage()['calls']}, report {out.name}")
        return

    if args.command == "once":
        agent.run_cycle(dry_run=args.dry_run)
    elif args.command == "loop":
        interval = args.interval or agent.rules.interval_minutes
        agent.run_forever(interval)
    elif args.command == "monitor":
        for action in agent.monitor.review():
            log(action)
    else:
        portfolio = agent.executor.portfolio()
        account = agent.executor.account_snapshot()
        equity = float(account.get("equity", 0) or 0)
        last_equity = float(account.get("last_equity", equity) or equity)
        loss_status = agent.loss_guard.evaluate(equity, last_equity)
        print(json.dumps({
            "mode": agent.rules.mode,
            "alpaca_cli": agent.executor.cli.version(),
            "account_number": account.get("account_number"),
            "market_open": agent.executor.market_is_open(),
            "equity": portfolio.equity,
            "cash": portfolio.cash,
            "options_buying_power": portfolio.options_buying_power,
            "open_positions": portfolio.open_positions,
            "committed_risk": portfolio.committed_risk,
            "loss_limits": loss_status.to_dict(),
            "reentry_locked": agent.lock.active(),
            "journal_entries": len(agent.executor.journal.load()),
            "cycles_run": agent.audit.cycle_count(),
        }, indent=2))


if __name__ == "__main__":
    main()
