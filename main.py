from __future__ import annotations

import argparse
import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents import AggressiveAgent, ConservativeAgent
from application import metrics_report, print_json, status_report
from backtest.execution_simulator import ExecutionAssumptions, HistoricalExecutionSimulator
from backtest.historical_data import build_alpaca_historical_data
from backtest.replay import ReplayEngine
from config.settings import Settings, load_settings
from core.models import AgentType, MarketSnapshot, PortfolioSnapshot, Signal, TradeIntent
from evaluation.reporting import comparison_table
from intelligence.codex_app_server import CodexAppServer
from intelligence.codex_client import CodexClient
from observability.jsonl import JsonlDecisionLogger
from persistence.database import SQLiteStore
from runtime.health import HealthRegistry
from runtime.orchestrator import RuntimeOrchestrator
from runtime.process_lock import RuntimeProcessLock


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Experimental Codex-assisted PAPER trading")
    result.add_argument("--settings", default="config/settings.yaml")
    commands = result.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one or more completed-candle paper cycles")
    run.add_argument("--agent", choices=["aggressive", "conservative", "all"], default="all")
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="send no broker orders (default)")
    mode.add_argument("--paper", action="store_true", help="explicitly allow Alpaca PAPER orders")
    run.add_argument("--once", action="store_true", help="run one cycle immediately and exit")

    backtest = commands.add_parser("backtest", help="replay the deterministic quant baseline")
    backtest.add_argument("--agent", choices=["aggressive", "conservative"], required=True)
    backtest.add_argument("--start", type=_date, required=True)
    backtest.add_argument("--end", type=_date, required=True)
    backtest.add_argument("--initial-capital", type=Decimal)
    backtest.add_argument("--symbols", nargs="+")
    backtest.add_argument("--codex", action="store_true", help="invoke Codex above threshold")
    backtest.add_argument("--codex-threshold", type=float)
    backtest.add_argument("--slippage-bps", type=Decimal)
    backtest.add_argument("--transaction-cost-bps", type=Decimal)
    backtest.add_argument("--fill-fraction", type=Decimal)

    commands.add_parser("status", help="show persisted runtime health")
    commands.add_parser("metrics", help="print complete metrics as JSON")
    commands.add_parser("report", help="print a concise experiment comparison")
    return result


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings(args.settings)
    store = SQLiteStore(settings.storage.database_url)
    try:
        if args.command == "status":
            _print_status(store)
        elif args.command == "metrics":
            print_json(metrics_report(store, settings))
        elif args.command == "report":
            print(comparison_table(metrics_report(store, settings)))
        elif args.command == "backtest":
            await _backtest(args, settings, store)
        elif args.command == "run":
            await _runtime(args, settings, store)
    finally:
        store.close()


async def _runtime(args: argparse.Namespace, settings: Settings, store: SQLiteStore) -> None:
    with RuntimeProcessLock(Path("runtime/trading.lock")):
        selected = _selected_agents(args.agent)
        logger = JsonlDecisionLogger(settings.logging.decisions.path)
        orchestrator = RuntimeOrchestrator(
            settings,
            store,
            logger,
            selected,
            paper_execution=bool(args.paper),
        )
        if args.once:
            try:
                await orchestrator.startup()
                cycle_ids = [await orchestrator.run_cycle(agent) for agent in selected]
                print_json({"mode": "paper" if args.paper else "dry-run", "cycles": cycle_ids})
            finally:
                await orchestrator.shutdown()
            return
        try:
            await orchestrator.run_forever()
        except KeyboardInterrupt:
            await orchestrator.shutdown()


async def _backtest(args: argparse.Namespace, settings: Settings, store: SQLiteStore) -> None:
    agent = AgentType(args.agent)
    universe = getattr(settings.market, agent.value)
    symbols = args.symbols or universe.universe
    configured = set(universe.universe)
    if not set(symbols) <= configured:
        raise SystemExit(f"symbols must be within the configured {agent.value} universe")
    key, secret = _credentials(agent)
    source = build_alpaca_historical_data(key, secret)
    duration = timedelta(minutes=15) if universe.timeframe == "15Min" else timedelta(hours=1)
    requested_start = args.start
    requested_end = args.end + timedelta(days=1)
    warmup_multiplier = 1 if agent is AgentType.AGGRESSIVE else 10
    data = source.load(
        symbols,
        timeframe=universe.timeframe,
        start=requested_start - duration * settings.backtest.warmup_bars * warmup_multiplier,
        end=requested_end,
    )
    assumptions = ExecutionAssumptions(
        slippage_bps=args.slippage_bps
        if args.slippage_bps is not None
        else Decimal(str(settings.backtest.slippage_bps)),
        transaction_cost_bps=args.transaction_cost_bps
        if args.transaction_cost_bps is not None
        else Decimal(str(settings.backtest.transaction_cost_bps)),
        fill_fraction=args.fill_fraction
        if args.fill_fraction is not None
        else Decimal(str(settings.backtest.fill_fraction)),
    )
    server = None
    decider = None
    if args.codex:
        server, decider = await _backtest_codex(settings, agent)
    threshold = (
        args.codex_threshold
        if args.codex_threshold is not None
        else settings.backtest.codex_signal_threshold
    )
    engine = ReplayEngine(
        settings,
        HistoricalExecutionSimulator(assumptions),
        codex_decider=decider,
        codex_signal_threshold=threshold,
    )
    capital = args.initial_capital or Decimal(str(settings.backtest.initial_capital))
    try:
        replay = await engine.run(
            data,
            agent,
            capital,
            decision_start=requested_start,
        )
    finally:
        if server is not None:
            await server.stop()
    summary = replay.deterministic_summary()
    run_record = {
        **summary,
        "start": requested_start,
        "end": args.end,
        "symbols": symbols,
        "initial_capital": capital,
        "timeframe": universe.timeframe,
        "codex_enabled": bool(args.codex),
        "codex_threshold": threshold,
        "codex_invocations": replay.codex_invocations,
        "codex_failure_count": sum(not decision.codex_valid for decision in replay.codex_decisions),
        "codex_trade_count": len(replay.codex_trades),
        "codex_final_equity": None
        if not replay.codex_snapshots
        else replay.codex_snapshots[-1].equity,
        "execution_assumptions": {
            "slippage_bps": assumptions.slippage_bps,
            "transaction_cost_bps": assumptions.transaction_cost_bps,
            "fill_fraction": assumptions.fill_fraction,
            "fill_timing": "next completed bar open",
        },
    }
    store.record("backtest_runs", run_record)
    if replay.benchmark_return is not None:
        initial = Decimal(str(settings.baselines["passive_benchmark"].initial_budget))
        store.record(
            "benchmark_snapshots",
            {
                "portfolio_id": "passive_benchmark",
                "source": "backtest_spy" if "SPY" in symbols else "backtest_equal_weight_universe",
                "timestamp": args.end,
                "equity": initial * (Decimal("1") + replay.benchmark_return),
                "drawdown_pct": 0,
            },
        )
    print_json(run_record)


async def _backtest_codex(
    settings: Settings,
    agent: AgentType,
) -> tuple[CodexAppServer, Any]:
    runtime = settings.codex.runtime
    server = CodexAppServer(
        os.environ.get("CODEX_EXECUTABLE", runtime.executable),
        Path(runtime.workspace_root) / "backtest" / agent.value,
        startup_timeout=runtime.startup_timeout_seconds,
        turn_timeout=runtime.turn_timeout_seconds,
    )
    threads = _EphemeralThreadManager()
    client = CodexClient(
        agent,
        server,
        threads,  # type: ignore[arg-type]
        settings.codex.threads[agent.value].prompt_file,
    )
    agent_settings = settings.agents[agent.value]
    trading_agent = (
        AggressiveAgent(client, agent_settings.decision.minimum_codex_confidence)
        if agent is AgentType.AGGRESSIVE
        else ConservativeAgent(client, agent_settings.decision.minimum_codex_confidence)
    )
    await server.start()

    async def decide(
        market: MarketSnapshot,
        signals: list[Signal],
        portfolio: PortfolioSnapshot,
    ) -> TradeIntent:
        return await trading_agent.create_intent(market, signals, portfolio)

    return server, decide


class _EphemeralThreadManager:
    def __init__(self) -> None:
        self.thread_id: str | None = None

    async def get_or_create(self, agent: AgentType, server: Any) -> str:
        if self.thread_id is None:
            self.thread_id = await server.start_thread()
        return self.thread_id

    def forget(self, agent: AgentType) -> None:
        self.thread_id = None


def _print_status(store: SQLiteStore) -> None:
    status = status_report(store)
    print("Environment: PAPER (live trading unavailable)")
    for line in HealthRegistry.from_store(store).human_lines():
        print(line)
    for portfolio_id, values in status["portfolios"].items():
        print(
            f"PORTFOLIO {portfolio_id}: equity={values['equity']} cash={values['cash']} "
            f"positions={values['positions']} reconciled={values['reconciled']}"
        )


def _selected_agents(value: str) -> list[AgentType]:
    if value == "all":
        return [AgentType.AGGRESSIVE, AgentType.CONSERVATIVE]
    return [AgentType(value)]


def _credentials(agent: AgentType) -> tuple[str, str]:
    prefix = "ALPACA_AGGRESSIVE" if agent is AgentType.AGGRESSIVE else "ALPACA_CONSERVATIVE"
    key = os.environ.get(f"{prefix}_API_KEY")
    secret = os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"missing Alpaca paper credentials for {agent.value}")
    return key, secret


def _date(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)


def main() -> None:
    asyncio.run(_run(parser().parse_args()))


if __name__ == "__main__":
    main()
