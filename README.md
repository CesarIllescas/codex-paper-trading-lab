# Codex Paper Trading Lab

Experimental Codex-assisted **paper-trading software** for comparing two Codex agents with
deterministic quant baselines. It is an evaluation system, not financial advice. Backtest
and paper results do not predict or guarantee live performance. Live trading has no supported
configuration or execution path.

The isolated experiments are:

- Aggressive Codex vs Aggressive Quant: BTC/USD, ETH/USD, SOL/USD on completed 15-minute bars.
- Conservative Codex vs Conservative Quant: SPY, QQQ, VTI on completed 1-hour bars during
  valid US equity sessions.
- A passive benchmark is tracked where data permits.

## Safety boundary

Startup fails unless `TRADING_ENV=paper`, `broker.paper: true`, the deterministic kill switch
is enabled, and all live, margin, leverage, shorting, options, and futures features remain
disabled. Every Alpaca `TradingClient` and `TradingStream` is explicitly created with
`paper=True`. Only `brokers/` can submit an order, and only after deterministic risk produces
an approved `OrderRequest`.

Codex cannot access Alpaca, submit orders, choose quantity, change risk parameters, or bypass
health/reconciliation gates. Its App Server subprocess receives an allowlisted environment
with Alpaca and other financial/API secrets removed. It runs from dedicated directories under
`runtime/codex/`, which are rejected if they contain an `.env` file. Codex failures produce
HOLD; the separate quant portfolio is never substituted for the Codex portfolio.

Submitted, accepted, partially filled, filled, cancelled, and rejected orders remain distinct.
Only actual fill updates affect broker-backed portfolio state. Unexplained reconciliation
differences, stale data, invalid prices, duplicate orders, unavailable streaming, unhealthy
dependencies, and risk breaches pause new paper orders.

## Architecture

```text
Completed Alpaca bars -> indicators -> deterministic strategy Signals
                                      |-> weighted Quant TradeIntent
                                      |      -> deterministic Risk -> local shadow portfolio
                                      |
                                      +-> Codex App Server -> Codex TradeIntent
                                             -> deterministic Risk -> Alpaca PAPER order
                                                       -> trade-update stream -> reconciliation

All paths -> cycle-scoped SQLite audit + secret-redacted JSONL -> metrics/reporting
```

- `backtest/`: historical loading, chronological replay, and deterministic execution modeling.
- `runtime/scheduler.py`: candle-boundary and US-session scheduling; it is not a fixed sleep loop.
- `runtime/orchestrator.py`: restart sequence and full autonomous decision cycles.
- `runtime/reconciliation.py`: broker-authoritative cash, position, order, and fill checks.
- `runtime/health.py`: dependency health and execution gates.
- `brokers/alpaca_stream.py`: normalized order events with bounded exponential reconnect.
- Existing `market/`, `strategies/`, `risk/`, and `portfolio/` logic is shared by replay and
  runtime rather than copied.

Each autonomous cycle has a UUID `decision_cycle_id`. Market snapshots, signals, quant and
Codex decisions, risk decisions, orders, order updates, fills, reconciliation, portfolio
snapshots, logs, and runtime failures use that ID wherever they belong to the cycle. This
allows the complete reason for a trade or rejection to be reconstructed.

## Install and configure

Python 3.12 or newer is required:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Alternatively, use the convenience requirements files. They reference `pyproject.toml`,
which remains the authoritative dependency definition:

```powershell
# Runtime dependencies only
python -m pip install -r requirements.txt

# Runtime plus pytest, Ruff, and other development tools
python -m pip install -r requirements-dev.txt
```

Install and authenticate the Codex CLI using its ChatGPT sign-in flow:

```powershell
codex --version
codex login
```

The intelligence integration uses persistent `codex app-server` subprocesses over stdio
JSONL; it does not use `codex exec`, the OpenAI API, Ollama, LangChain, LangGraph, or another
LLM. See the [official Codex App Server documentation](https://learn.chatgpt.com/docs/app-server).

Create separate Alpaca paper credentials for aggressive and conservative accounts. Set them
in the process environment; the application deliberately does not load `.env` files:

Copy `.env.example` to `.env` and put the PAPER credentials and Codex executable
there. The CLI automatically loads this file from the repository root. Existing
shell environment variables take precedence over values in `.env`.

```powershell
Copy-Item .env.example .env
notepad .env
```

For example:

```dotenv
TRADING_ENV=paper
ALPACA_AGGRESSIVE_API_KEY=<paper-key>
ALPACA_AGGRESSIVE_SECRET_KEY=<paper-secret>
ALPACA_CONSERVATIVE_API_KEY=<paper-key>
ALPACA_CONSERVATIVE_SECRET_KEY=<paper-secret>
CODEX_EXECUTABLE=codex
```

Confirm `Get-Command codex` works in PowerShell. Prefer `CODEX_EXECUTABLE=codex`
because VS Code extension directories contain version numbers and change during
updates. If `codex` is not on PATH and an absolute Windows path is required, use
forward slashes and update the path after upgrading the extension. Never place
`.env` inside `runtime/codex/`; the root `.env` is ignored by Git and is never
forwarded to the Codex App Server subprocess.

Credentials are needed in dry-run and backtest modes because Alpaca supplies account and/or
market data. Automated tests use fakes and need neither credentials nor network access.

## Three execution modes

Backtesting chronologically replays historical completed bars and simulates fills locally.
It never sends orders. The end date is inclusive:

```powershell
python main.py backtest --agent aggressive --start 2026-01-01 --end 2026-06-30
python main.py backtest --agent conservative --start 2026-01-01 --end 2026-06-30
```

Override capital, configured symbols, and simulation assumptions when needed:

```powershell
python main.py backtest --agent aggressive --start 2026-01-01 --end 2026-06-30 `
  --initial-capital 25000 --symbols BTC/USD ETH/USD `
  --slippage-bps 3 --transaction-cost-bps 1 --fill-fraction 0.75
```

The default replay runs only the deterministic quant baseline. Optional Codex replay uses a
separate brokerless simulated portfolio and an ephemeral replay thread. Codex is called only
when the quant signal is non-HOLD and meets the configured threshold:

```powershell
python main.py backtest --agent aggressive --start 2026-01-01 --end 2026-06-30 `
  --codex --codex-threshold 0.75
```

Dry-run is the autonomous runtime default. It performs reconciliation, market data,
strategies, both decisions, risk, persistence, local quant simulation, health, and metrics,
but sends no broker orders:

```powershell
python main.py run --agent aggressive --dry-run
python main.py run --agent all --dry-run
python main.py run --agent conservative --dry-run --once
```

Actual Alpaca paper order submission requires the explicit `--paper` flag:

```powershell
python main.py run --agent aggressive --paper
python main.py run --agent all --paper
python main.py run --agent aggressive --paper --once
```

Omitting both mode flags is dry-run. There is no `--live` option. `--once` performs startup
recovery and one immediate completed-bar decision pass; without it, decisions are scheduled
around candle boundaries until interrupted.

## Replay and execution assumptions

At replay time T, a strategy receives only candles completed by T. Warmup bars can populate
indicators but cannot generate pre-start trades. A market decision at one completed bar fills
at the next bar's open, with deterministic direction-aware slippage, transaction costs, and
an optional fill fraction. A fill fraction below one models partial fills and partial exits;
the unfilled remainder is cancelled in this initial simulator.

This is deliberately an approximation. Bar data does not reveal intrabar queue position,
spread evolution, market impact, latency, or exact fill availability. Corporate actions and
venue-specific microstructure may also differ from the model. The simulator must not be
interpreted as an exact reconstruction of executable historical prices.

## Autonomous scheduling and restart recovery

Aggressive cycles follow completed UTC-aligned 15-minute crypto candles plus a small grace
period. Conservative cycles use Alpaca's market calendar and hourly boundaries anchored to
valid US sessions. Incomplete bars are filtered by the market normalization layer.

Startup proceeds in this order for each selected account:

1. validate paper-only configuration and open SQLite;
2. connect to the Alpaca paper account and reconcile broker/database state;
3. establish the trade-update stream;
4. initialize Codex App Server;
5. resume the persisted aggressive or conservative thread ID;
6. verify health before any paper order can pass its final gate.

Reconciliation runs at startup, before every cycle, periodically, and after stream reconnects
or terminal/fill events. Broker state is authoritative. Explained stream fills update the
stored snapshot; unexplained cash, positions, or open-order differences mark the portfolio
unhealthy and pause new orders. Codex memory is never used as portfolio storage, and every
post-restart turn receives current broker, portfolio, market, indicator, and signal state.

## Health, status, and reports

Show the most recently persisted health state and portfolio summaries:

```powershell
python main.py status
```

Health covers SQLite, the kill switch, each Codex App Server/thread, each Alpaca paper
connection, market freshness, each trade stream, reconciliation, and last successful cycle.
A critical unhealthy or unknown dependency blocks new orders while audit and shadow paths
remain isolated.

Print raw metrics or a concise comparison table:

```powershell
python main.py metrics
python main.py report
```

Portfolio reporting includes initial/current equity, return, realized/unrealized P&L,
drawdown, Sharpe, Sortino, trades, win rate, profit factor, exposure, and turnover. Codex
contribution reporting includes agreement, overrides, profitable overrides, performance after
agreement/override, HOLD frequency, average confidence, rejected decisions, and confidence
buckets. Small samples are descriptive; the software does not claim statistical significance.

## Persistence and validation

SQLite stores audit events, order/fill state, broker and shadow portfolios, equity history,
reconciliation and health events, backtest runs, benchmarks, metrics, and Codex thread IDs.
`logs/decisions.jsonl` is secret-redacted and includes HOLD, rejection, and failed-closed
cycles.

Run the fully offline suite:

```powershell
pytest
ruff check .
ruff format --check .
```

## Intentionally deferred

Phase 2 does not include live trading, margin, leverage, shorting, options, futures, news,
sentiment, a dashboard, another LLM, strategy/risk self-modification, or claims of live-market
fitness. Those are not silently enabled by any runtime or backtest option.
