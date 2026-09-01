# Dailies, dailies, watch it work

An autonomous options trading agent that reasons from macro news narrative rather than price indicators.

It reads the financial news wire, forms a directional thesis per market sector, and expresses that thesis
as a defined-risk vertical credit spread on the matching sector ETF. Every position is capped, sized
against account equity, and managed to tiered profit targets or a volatility-scaled stop without human
input.

Built for the Alpaca AI Trading Agents Hackathon. Paper trading only.

## Why news instead of indicators

Most trading agents read the chart. This one reads the story. A headline about OPEC supply cuts is
information about XLE before it is a candlestick, and a language model is a better instrument for
reading a paragraph than a moving average is.

The agent maps nine macro sectors onto liquid ETFs, buckets each incoming article by ticker tag and
keyword, and asks the model one question per sector: where does this trade over the next two to six
weeks, and how sure are you?

Conviction is the throttle. Below the floor, the agent does nothing. Doing nothing is a position.

## The separation that matters

The language model proposes. Deterministic code disposes. Every hard limit lives in `risk_rules.json`,
outside the model's reach, and no thesis can argue its way past one no matter how convincing it reads.

A model asked whether a really strong thesis justifies overriding a stop will sometimes say yes.
So it is never asked.

## Alpaca infrastructure

Every trading action goes through the Alpaca CLI as a subprocess returning structured JSON: account
and clock reads, position and order listings, order status reconciliation, and multi-leg option orders
submitted with `--order-class mleg`. The CLI's own `--dry-run` flag is used to validate an order body
against Alpaca before anything is sent for real.

Market data uses the Alpaca SDK, since chains, quotes and the news wire are reads the CLI does not
cover. The division is deliberate: reads through the SDK, every write through the CLI.

Going live is likewise not a command-line flag. `execution.mode` in `risk_rules.json` starts as
`dry_run`, and the agent refuses to place any order until a human edits that file. A flag is one typo
away from real orders.

## Strategy

Direction becomes structure:

- bullish thesis becomes a put credit spread below the market
- bearish thesis becomes a call credit spread above the market
- neutral thesis produces no trade

Credit spreads are chosen deliberately over long options. Maximum loss is known at entry, which is what
makes the risk gates meaningful, and short premium does not bleed to theta while a thesis plays out.

Short strike targets roughly 30 delta. Because the paper options feed returns no greeks, the agent
computes its own: Black-Scholes implied volatility solved from the quoted mid by bisection, then delta
from that vol. Contracts whose implied vol cannot be recovered are discarded rather than guessed at.

## Risk gates

Every gate must pass or the trade is refused and the reason written to the audit log.

| Gate | Default |
| --- | --- |
| Conviction floor | 0.55 |
| Daily loss limit | 5 percent of starting capital, halts entries |
| Weekly loss limit | 10 percent of starting capital, halts entries |
| Max risk per trade | 2 percent of equity |
| Max portfolio risk | 10 percent of equity |
| Cash buffer held | 25 percent of equity |
| Max open positions | 5 |
| Max per underlying | 1 |
| Days to expiry window | 7 to 45 |
| Minimum credit to width | 0.20 |
| Maximum quoted spread | 65 percent of mid |
| Take profit tiers | 35 / 55 / 75 percent of credit |
| Stop loss | volatility scaled, 1.5x to 3.0x credit |
| Expiry guard | close at 2 DTE |
| Re-entry lock | 48h after a gain, 120h after a loss |

Loss limits are measured against `starting_capital_usd`, not live equity. Measuring against live equity
shrinks the risk budget exactly when the account is losing, which is when it should hold still.

A loss-limit breach halts new entries only. Stops, take-profits and the expiry guard keep running:
risk reduction is never halted.

Position size is the largest whole number of contracts whose combined maximum loss fits inside the
per-trade budget, the remaining portfolio risk cap, and the cash buffer. Whichever binds first wins.
Sizing works from cash, never from the 4x margin buying power the account reports.

## Position management

Take-profit is tiered rather than all-or-nothing: a third of the position closes at 35 percent of
credit captured, half the remainder at 55 percent, the rest at 75 percent. Each tier fires once per
holding period. This banks gains progressively while leaving something on for a thesis that keeps
working.

The stop is scaled to the underlying's own implied volatility at entry, clamped between 1.5x and 3.0x
credit. A quiet name gets a tight stop, a jumpy one gets room, so ordinary noise does not close a
thesis that is still intact.

After any close, the underlying is locked from re-entry: 48 hours if it closed at a gain, 120 hours if
at a loss. Without this the agent would stop out of a sector and re-enter it on the next cycle off the
same headline that was just proven wrong.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
```

Fill `.env` with Alpaca paper keys and a Featherless key. Never commit it.

The agent trades through the [Alpaca CLI](https://github.com/alpacahq/cli). Put the binary at
`tools/alpaca` (or `tools/alpaca.exe`), on your PATH, or point `ALPACA_CLI_PATH` at it:

```bash
go install github.com/alpacahq/cli/cmd/alpaca@latest
```

Prebuilt binaries for macOS, Linux and Windows are on the CLI's releases page. The CLI reads the same
`ALPACA_API_KEY` and `ALPACA_SECRET_KEY` names this project already uses, and the adapter passes them
per invocation so nothing is written to disk.

## Usage

```bash
python main.py rules
python main.py status
python main.py once
python main.py loop --interval 30
python main.py monitor
```

`once` and `loop` simulate while `execution.mode` is `dry_run`. Set it to `live` in `risk_rules.json`
when ready to trade. `--aggression conservative|moderate|aggressive` retunes the conviction floor,
position cap and risk budgets in one move.

`loop` takes its cadence from `cadence.default_interval_minutes` unless `--interval` overrides it.

## Model budget

Inference is metered, so the agent treats model calls as a resource rather than something free.

A sector below `signal.min_articles_per_sector` gets no model call at all. Thin coverage reliably
produced neutral, low-conviction theses, so those calls were spend without signal.

Cadence is set wide on purpose. Sector narratives do not turn over in half an hour, and a run that
exhausts its inference budget before the period ends stops trading entirely.

Credit exhaustion is treated as a distinct failure, not a transient one. An auth or billing response
raises immediately rather than retrying, and the cycle records `llm_unavailable` and returns without
opening positions. The failure mode to avoid is an agent that keeps looping and quietly reports no
candidates because the model stopped answering. Every cycle logs its call count and token usage.

## Layout

```
daily_spread/
  config.py     settings, sector to ETF universe, aggression profiles
  rules.py      risk_rules.json loader, hard limits, mode gate
  ingest.py     Alpaca news wire, sector bucketing
  llm.py        Featherless client with retry and JSON extraction
  signal.py     thesis engine, one structured call per sector
  pricing.py    Black-Scholes, implied vol solver, delta
  market.py     quotes, chain retrieval, vertical spread construction
  risk.py       gates and position sizing
  guards.py     loss limits, re-entry locks
  cli.py        Alpaca CLI adapter, all trading goes through here
  execute.py    multi-leg order submission, trade journal
  monitor.py    tiered take-profit, scaled stop, expiry guard
  audit.py      staged JSONL trade log
  agent.py      the cycle
main.py         command line entry point
risk_rules.json hard limits, edited by humans only
```

## Audit trail

Every cycle appends staged records to `state/trade_log.jsonl`: `screened`, `thesis`, `risk_check`,
`order`, `cycle`. Each rejection carries its reason, so the log shows not just what the agent traded
but everything it considered and declined, and why. Full cycle snapshots land in `logs/`.

`state/journal.json` tracks open spreads and remaining contracts, and is reconciled against live
broker positions on every pass.

## Attribution

Risk design adapted from [FriesTrader](https://github.com/YizhiSong/FriesTrader) (MIT): limits held
outside the model's reach, loss limits measured against a non-drifting capital base, a human-only
dry-run to live gate, tiered take-profits, and re-entry locks after a close.

## Disclosure

Paper trading results are hypothetical and are not indicative of future results. Options carry risk,
including the loss of principal. This is not investment advice.
