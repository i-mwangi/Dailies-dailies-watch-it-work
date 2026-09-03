import argparse
import json

from daily_spread.agent import build_agent, log
from daily_spread.rules import load_rules


def render_report(agent) -> None:
    import collections

    account = agent.executor.account_snapshot()
    equity = float(account.get("equity", 0) or 0)
    cash = float(account.get("cash", 0) or 0)
    start = agent.rules.starting_capital

    print()
    print("  DAILIES, DAILIES, WATCH IT WORK")
    print("  " + "-" * 62)
    print(f"  account {account.get('account_number')}    mode {agent.rules.mode}    "
          f"cli {agent.executor.cli.version()}")
    print()
    print(f"  equity      ${equity:>12,.2f}   ({equity - start:+,.2f} from ${start:,.0f})")
    print(f"  cash        ${cash:>12,.2f}   (${cash - start:+,.2f} credit collected)")

    positions = agent.executor.option_positions()
    unrealised = sum(float(p.get("unrealized_pl") or 0) for p in positions)
    print(f"  open mark   ${unrealised:>12,.2f}   ({len(positions)} legs)")
    print()

    entries = [e for e in agent.executor.journal.load() if e.get("status") == "open"]
    if entries:
        print("  OPEN SPREADS")
        print(f"    {'sym':<6}{'structure':<22}{'qty':>5}{'credit':>9}{'max loss':>11}{'stop':>8}")
        risk = 0.0
        for e in entries:
            qty = int(e.get("contracts_remaining", e.get("contracts", 0)))
            loss = float(e.get("max_loss", 0)) * qty
            risk += loss
            print(f"    {e['underlying']:<6}{e['strategy'].replace('_',' '):<22}{qty:>5}"
                  f"{e['credit']:>9.2f}{loss:>11,.0f}"
                  f"{agent.monitor.stop_multiple(e):>7.2f}x")
        cap = equity * agent.settings.max_total_risk_pct
        print(f"    {'':<6}{'committed risk':<22}{'':>5}{'':>9}{risk:>11,.0f}"
              f"   of {cap:,.0f} cap")
        print()

    stages = collections.Counter()
    calls = 0
    for line in open(agent.audit.path, encoding="utf-8"):
        rec = json.loads(line)
        stages[rec.get("stage")] += 1
        if rec.get("stage") == "cycle":
            calls += rec.get("llm_calls") or 0
    print("  AUTONOMOUS ACTIVITY")
    print(f"    cycles {stages['cycle']}    theses {stages['thesis']}    "
          f"risk checks {stages['risk_check']}    orders {stages['order']}    "
          f"model calls {calls}")
    print()

    rejects = collections.Counter()
    for line in open(agent.audit.path, encoding="utf-8"):
        rec = json.loads(line)
        if rec.get("stage") == "risk_check" and not rec.get("passed"):
            rejects[rec.get("reason", "")[:44]] += 1
    if rejects:
        print("  WHY TRADES WERE REFUSED")
        for reason, n in rejects.most_common(5):
            print(f"    {n:>4}  {reason}")
        print()

    base = agent.settings.log_dir
    pairs = [("no memory", base / "backtest_30d_h10.json"),
             ("with memory", base / "backtest_30d_h10_mem.json")]
    loaded = [(tag, json.loads(p.read_text(encoding="utf-8"))) for tag, p in pairs if p.exists()]
    if loaded:
        print("  SIGNAL BACKTEST, 10 DAY HORIZON")
        print(f"    {'':<16}{'n':>6}{'hit':>8}{'baseline':>10}{'edge':>8}{'mean ret':>10}")
        for tag, d in loaded:
            o, b = d["overall"], d["baseline_always_long"]
            edge = o["hit_rate"] - b["always_long_hit_rate"]
            print(f"    {tag:<16}{o['count']:>6}{o['hit_rate']:>8.3f}"
                  f"{b['always_long_hit_rate']:>10.3f}{edge:>+8.3f}"
                  f"{o['mean_signed_return']:>+10.4f}")
        print()
        print("    Measured against an always-long baseline. The raw signal underperformed;")
        print("    outcome memory closed part of the gap but not all of it.")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(prog="daily-spread")
    parser.add_argument("command", choices=["once", "loop", "status", "monitor", "rules", "backtest", "report"])
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

    if args.command == "report":
        render_report(agent)
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
