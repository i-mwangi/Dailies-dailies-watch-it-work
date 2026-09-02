# Dailies, dailies, watch it work

Alpaca paper account **PA3QBU3CI6Q8** · repo <https://github.com/i-mwangi/Dailies-dailies-watch-it-work>

An autonomous options agent that reads the financial news wire, forms a macro thesis per market
sector, and expresses that thesis as a defined-risk vertical credit spread on the matching sector
ETF. It sizes, places, monitors and closes every position without a human in the loop.

## AI logic

Nine macro sectors map onto liquid ETFs — Energy to XLE, Inflation and Bonds to TLT, Financials to
XLF, and so on. Each cycle pulls the Alpaca news wire, buckets articles by the ticker tags Alpaca
attaches plus keyword matching, and asks the model one structured question per sector: where does
this sector trade over the next two to six weeks, and how confident are you?

The model returns JSON — direction, conviction 0 to 1, horizon, thesis, and the single most likely
way the thesis is wrong. Direction becomes structure: a bullish thesis becomes a put credit spread
below the market, bearish becomes a call credit spread above it, neutral produces no trade.
Conviction is the throttle; below the floor the agent does nothing, and doing nothing is a position.

Inference runs on Featherless (Qwen2.5-72B). Model calls are treated as a metered resource: a sector
with fewer than four articles gets no call at all, because thin coverage reliably produced neutral
low-conviction output, and credit exhaustion raises immediately rather than retrying so the agent
fails loudly instead of quietly reporting no candidates.

The agent also remembers. Every close writes an outcome record, and the last five outcomes for a
sector are replayed into the next thesis prompt for that sector — including how often it was wrong.

## Risk gates

The model proposes; deterministic code disposes. Every hard limit lives in `risk_rules.json`,
outside the model's reach, and no thesis argues its way past one. A model asked whether a strong
thesis justifies overriding a stop will sometimes say yes, so it is never asked.

| Gate | Setting |
| --- | --- |
| Conviction floor | 0.55 |
| Allowed directions | bullish only (see Measurement) |
| Daily / weekly loss limit | 5% / 10% of starting capital, halts entries |
| Max risk per trade | 2% of equity |
| Max portfolio risk | 10% of equity |
| Cash buffer | 25% of equity held back |
| Max open positions | 5, one per underlying |
| Stop-loss guard | 3 stop-outs in 24h locks entries 12h |
| Drawdown guard | 8% peak-to-trough locks entries 12h |
| DTE window | 7 to 45 days |
| Take profit | tiered at 35 / 55 / 75% of credit |
| Stop loss | volatility scaled, 1.5x to 3.0x credit |
| Expiry guard | close at 2 DTE |
| Re-entry lock | 48h after a gain, 120h after a loss |

Three details matter more than the numbers. **Loss limits measure against starting capital, not live
equity** — measuring against equity shrinks the risk budget exactly when the account is losing.
**Breaches halt entries only**; stops, take-profits and the expiry guard keep running, because risk
reduction is never halted. **Going live is a human edit**, not a flag: `execution.mode` starts at
`dry_run` and the agent refuses to place any order until a person changes that file.

Because the paper options feed returns no greeks, the agent computes its own — Black-Scholes implied
volatility solved from the quoted mid by bisection, then delta from that vol. Contracts whose
implied volatility cannot be recovered are discarded rather than guessed at. Stops are then scaled
to each position's own implied volatility, so a quiet underlying gets a tight stop and a jumpy one
gets room.

## Alpaca infrastructure

Every write goes through the **Alpaca CLI** (v0.0.14) as a subprocess returning structured JSON:
`account get`, `clock`, `position list`, `order list` and `order get` for fill reconciliation, and
`order submit --order-class mleg --legs` for the spreads themselves. The CLI's own `--dry-run` flag
validates an order body against Alpaca before anything is sent for real. Credentials are passed per
invocation through the environment, never written to a profile on disk.

Reads the CLI does not cover — option chains, quotes, daily bars and the news wire — use the Alpaca
SDK. The division is deliberate: reads through the SDK, every write through the CLI.

Every cycle appends staged records to `trade_log.jsonl`: `screened`, `thesis`, `risk_check`,
`order`, `cycle`. Each rejection carries its reason, so the log shows not just what the agent traded
but everything it declined and why.

## Measurement

The signal was tested rather than assumed. A backtest replays historical news through the same
thesis engine and measures the underlying's actual forward move, scored against an always-long
baseline — sector ETFs drift upward, so a bullish bias scores well on raw hit rate while having no
skill.

The raw signal underperformed. Across 5, 10 and 20 trading-day horizons the edge against baseline
was consistently about **-11 points**, and absolute results that improved with horizon were market
drift rather than skill. Two findings were stable across all three horizons: **conviction ran
backwards**, with 0.70+ calls less accurate than 0.60-0.70, and **bearish theses hit roughly 31%**
against bullish calls that tracked the underlying.

Two changes followed from that evidence. Bearish entries were disabled. The outcome memory described
above was added, then A/B tested on matched observations — same date and sector in both runs:

| Matched sample, 10-day horizon | No memory | With memory |
| --- | --- | --- |
| Directional calls | 69 | 58 |
| Hit rate | 43.5% | 48.3% |
| Mean signed return | -0.08% | **+0.64%** |
| Edge vs baseline | -0.130 | **-0.086** |

Memory improved the edge by 4.4 points and turned mean return positive, mainly by making the agent
decline to commit: 29 calls were downgraded to neutral against 18 upgraded. It did not close the
gap. The honest reading is that this is a short-premium harvesting strategy whose news layer is now
roughly neutral rather than harmful, and that hit rate and mean return can diverge — for a credit
spread, which profits when the underlying does not move against it, the second matters more.

## Live state

18 autonomous cycles, 513 model calls, 11 orders placed and filled, 73 risk checks of which 11
approved. The most common rejections: 31 neutral theses, 11 where no spread cleared the construction
filters, 15 blocked by the one-position-per-underlying rule, 4 by the position cap.

Five spreads open on TLT, XLE, GLD, XLF and SPY. Committed risk $4,602 against a $10,000 portfolio
cap. Equity $99,626, cash $101,556 against $100,000 started — $1,556 of credit collected against
$370 of open mark-to-market.

Paper trading results are hypothetical and are not indicative of future results. Options carry risk,
including loss of principal. This is not investment advice.
