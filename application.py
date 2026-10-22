from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents import AggressiveAgent, ConservativeAgent
from brokers.alpaca import AlpacaBrokerAdapter, build_alpaca_broker
from config.settings import Settings
from core.models import (
    AgentType,
    ExecutedTrade,
    MarketSnapshot,
    OrderStatus,
    PortfolioSnapshot,
    StrategyContext,
)
from evaluation.comparison import comparison_metrics, portfolio_metrics
from intelligence.codex_app_server import CodexAppServer
from intelligence.codex_client import CodexClient
from intelligence.codex_threads import CodexThreadManager
from market.prices import AlpacaPriceProvider, build_alpaca_price_provider
from observability.jsonl import JsonlDecisionLogger
from persistence.database import SQLiteStore
from portfolio.shadow import ShadowPortfolio
from risk import AggressiveRiskEngine, ConservativeRiskEngine
from risk.kill_switch import KillSwitchState
from strategies import mean_reversion_signal, momentum_signal, trend_following_signal
from strategies.baseline import deterministic_baseline


def _portfolio_ids(agent: AgentType) -> tuple[str, AgentType]:
    if agent is AgentType.AGGRESSIVE:
        return "aggressive_codex", AgentType.AGGRESSIVE_QUANT
    return "conservative_codex", AgentType.CONSERVATIVE_QUANT


def _credentials(agent: AgentType) -> tuple[str, str]:
    prefix = "ALPACA_AGGRESSIVE" if agent is AgentType.AGGRESSIVE else "ALPACA_CONSERVATIVE"
    key = os.environ.get(f"{prefix}_API_KEY")
    secret = os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"missing Alpaca paper credentials for {agent.value}")
    return key, secret


def _signals(settings: Settings, agent: AgentType, market: MarketSnapshot) -> list[Any]:
    context = StrategyContext(
        agent=agent,
        market=market,
        indicator_values=market.indicators,
    )
    toggles = settings.agents[agent.value].strategies
    functions = {
        "momentum": momentum_signal,
        "mean_reversion": mean_reversion_signal,
        "trend_following": trend_following_signal,
    }
    return [
        functions[name](context)
        for name, toggle in toggles.items()
        if toggle.enabled and name in functions
    ]


def _restore_shadow(
    store: SQLiteStore,
    portfolio_id: AgentType,
    initial_cash: Decimal,
) -> ShadowPortfolio:
    raw = store.latest_portfolio_snapshot(portfolio_id.value)
    if raw is None:
        return ShadowPortfolio(portfolio_id, initial_cash)
    return ShadowPortfolio.from_snapshot(
        portfolio_id,
        initial_cash,
        PortfolioSnapshot.model_validate(raw),
    )


def _broker_snapshot(
    store: SQLiteStore,
    broker: AlpacaBrokerAdapter,
    portfolio_id: str,
) -> PortfolioSnapshot:
    raw = store.latest_portfolio_snapshot(portfolio_id)
    previous = None if raw is None else PortfolioSnapshot.model_validate(raw)
    return broker.account_snapshot(
        portfolio_id,
        peak_equity=None if previous is None else previous.peak_equity,
        expected_positions=None if previous is None else previous.positions,
        realized_pnl=Decimal("0") if previous is None else previous.realized_pnl,
    )


async def run_agent_once(
    settings: Settings,
    agent_type: AgentType,
    *,
    dry_run: bool,
    store: SQLiteStore,
    logger: JsonlDecisionLogger,
) -> list[dict[str, Any]]:
    agent_settings = settings.agents[agent_type.value]
    universe_settings = getattr(settings.market, agent_type.value)
    key, secret = _credentials(agent_type)
    provider: AlpacaPriceProvider = build_alpaca_price_provider(key, secret)
    broker = build_alpaca_broker(agent_type)
    codex_portfolio_id, quant_type = _portfolio_ids(agent_type)
    codex_portfolio = _broker_snapshot(store, broker, codex_portfolio_id)
    baseline_config = settings.baselines[quant_type.value]
    shadow = _restore_shadow(
        store,
        quant_type,
        Decimal(str(baseline_config.initial_budget)),
    )
    now = datetime.now(UTC)
    markets: dict[str, MarketSnapshot] = {}
    for symbol in universe_settings.universe:
        markets[symbol] = provider.get_snapshot(
            symbol,
            timeframe=universe_settings.timeframe,
            stale_after_seconds=settings.market.stale_data_seconds,
            decision_timestamp=now,
        )
    prices = {symbol: market.price for symbol, market in markets.items()}
    shadow_snapshot = shadow.snapshot(prices, now)

    runtime = settings.codex.runtime
    server = CodexAppServer(
        os.environ.get("CODEX_EXECUTABLE", runtime.executable),
        Path(runtime.workspace_root) / agent_type.value,
        startup_timeout=runtime.startup_timeout_seconds,
        turn_timeout=runtime.turn_timeout_seconds,
    )
    thread_manager = CodexThreadManager(store)
    prompt = settings.codex.threads[agent_type.value].prompt_file
    codex_client = CodexClient(agent_type, server, thread_manager, prompt)
    agent = (
        AggressiveAgent(codex_client, agent_settings.decision.minimum_codex_confidence)
        if agent_type is AgentType.AGGRESSIVE
        else ConservativeAgent(codex_client, agent_settings.decision.minimum_codex_confidence)
    )
    risk_engine = (
        AggressiveRiskEngine(agent_settings.risk)
        if agent_type is AgentType.AGGRESSIVE
        else ConservativeRiskEngine(agent_settings.risk)
    )
    weights = {
        name: toggle.weight for name, toggle in agent_settings.strategies.items() if toggle.enabled
    }
    results: list[dict[str, Any]] = []
    try:
        for symbol, market in markets.items():
            signals = _signals(settings, agent_type, market)
            for item in signals:
                store.record("strategy_signals", item)
            recent = store.records("decisions")[-5:]
            codex_intent = await agent.create_intent(
                market, signals, codex_portfolio, recent_decisions=recent
            )
            quant_intent = deterministic_baseline(quant_type, signals, weights)
            codex_duplicate = store.has_recent_order(
                agent_type.value,
                symbol,
                codex_intent.action.value,
                settings.execution.duplicate_order_window_seconds,
                now,
            )
            quant_duplicate = store.has_recent_order(
                quant_type.value,
                symbol,
                quant_intent.action.value,
                settings.execution.duplicate_order_window_seconds,
                now,
            )
            codex_risk = risk_engine.evaluate(
                codex_intent,
                codex_portfolio,
                market,
                KillSwitchState(duplicate_order=codex_duplicate),
            )
            quant_risk = risk_engine.evaluate(
                quant_intent,
                shadow_snapshot,
                market,
                KillSwitchState(duplicate_order=quant_duplicate),
            )
            store.record("codex_decisions", codex_intent)
            store.record("risk_decisions", codex_risk)
            store.record("risk_decisions", quant_risk)

            broker_state = None
            if not dry_run and codex_risk.approved and codex_risk.order_request is not None:
                store.record(
                    "orders",
                    {
                        **codex_risk.order_request.model_dump(mode="json"),
                        "status": OrderStatus.SUBMITTED.value,
                    },
                )
                broker_state = broker.submit_approved(codex_risk)
                store.record(
                    "orders",
                    {
                        **asdict(broker_state),
                        "agent": agent_type.value,
                        "action": codex_intent.action.value,
                    },
                )
                if broker_state.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
                    fill_record = {
                        "fill_id": f"alpaca-{broker_state.order_id}",
                        "order_id": broker_state.order_id,
                        "agent": agent_type.value,
                        "symbol": symbol,
                        "action": codex_intent.action.value,
                        "quantity": str(broker_state.filled_quantity),
                        "price": None
                        if broker_state.average_fill_price is None
                        else str(broker_state.average_fill_price),
                        "executed_at": now.isoformat(),
                        "status": broker_state.status.value,
                        "fees": "0",
                    }
                    if broker_state.average_fill_price is not None:
                        fill_record["trade_id"] = fill_record["fill_id"]
                    store.record("fills", fill_record)

            shadow_trade = None
            if not dry_run and quant_risk.approved:
                shadow_trade = shadow.execute(quant_risk, market.price, now)
                store.record("orders", quant_risk.order_request.model_dump(mode="json"))
                store.record("fills", shadow_trade)
                shadow_snapshot = shadow.snapshot(prices, now)

            decision_record = {
                "timestamp": now.isoformat(),
                "agent": agent_type.value,
                "symbol": symbol,
                "market_snapshot_id": market.snapshot_id,
                "market_price": str(market.price),
                "strategy_signals": [item.model_dump(mode="json") for item in signals],
                "codex_action": codex_intent.action.value,
                "quant_action": quant_intent.action.value,
                "confidence": codex_intent.confidence,
                "codex_rejected": not codex_risk.approved and codex_intent.action.value != "HOLD",
                "codex_risk": codex_risk.model_dump(mode="json"),
                "quant_risk": quant_risk.model_dump(mode="json"),
                "broker_status": None if broker_state is None else broker_state.status.value,
                "shadow_fill": None
                if shadow_trade is None
                else shadow_trade.model_dump(mode="json"),
                "dry_run": dry_run,
                "return": None,
            }
            store.record("decisions", decision_record)
            logger.log("trading_decision", decision_record)
            results.append(decision_record)
        store.record("portfolio_snapshots", codex_portfolio)
        store.record("portfolio_snapshots", shadow_snapshot)
        for snapshot in (codex_portfolio, shadow_snapshot):
            for position in snapshot.positions:
                store.record(
                    "positions",
                    {
                        "portfolio_id": snapshot.portfolio_id,
                        "timestamp": snapshot.timestamp.isoformat(),
                        **position.model_dump(mode="json"),
                    },
                )
        store.record(
            "equity_history",
            {
                "portfolio_id": codex_portfolio.portfolio_id,
                "timestamp": codex_portfolio.timestamp.isoformat(),
                "equity": str(codex_portfolio.equity),
            },
        )
        store.record(
            "equity_history",
            {
                "portfolio_id": shadow_snapshot.portfolio_id,
                "timestamp": shadow_snapshot.timestamp.isoformat(),
                "equity": str(shadow_snapshot.equity),
            },
        )
        return results
    finally:
        await server.stop()


def status_report(store: SQLiteStore) -> dict[str, Any]:
    portfolios: dict[str, Any] = {}
    for portfolio_id in (
        "aggressive_codex",
        "conservative_codex",
        "aggressive_quant",
        "conservative_quant",
    ):
        latest = store.latest_portfolio_snapshot(portfolio_id)
        if latest:
            portfolios[portfolio_id] = {
                "timestamp": latest["timestamp"],
                "cash": latest["cash"],
                "equity": latest["equity"],
                "positions": len(latest.get("positions", [])),
                "reconciled": latest.get("reconciled"),
            }
    return {
        "environment": "paper",
        "portfolios": portfolios,
        "codex_threads": {
            name: store.get_thread_id(name) is not None for name in ("aggressive", "conservative")
        },
    }


def metrics_report(store: SQLiteStore, settings: Settings) -> dict[str, Any]:
    raw_snapshots = store.records("portfolio_snapshots")
    raw_fills = store.records("fills")
    result: dict[str, Any] = {}
    initial = {
        "aggressive_codex": settings.agents["aggressive"].portfolio.initial_budget,
        "conservative_codex": settings.agents["conservative"].portfolio.initial_budget,
        "aggressive_quant": settings.baselines["aggressive_quant"].initial_budget,
        "conservative_quant": settings.baselines["conservative_quant"].initial_budget,
    }
    for portfolio_id, initial_equity in initial.items():
        snapshots = [
            PortfolioSnapshot.model_validate(
                {key: value for key, value in item.items() if key != "decision_cycle_id"}
            )
            for item in raw_snapshots
            if item.get("portfolio_id") == portfolio_id
        ]
        trades = []
        agent_value = portfolio_id.removesuffix("_codex")
        for item in raw_fills:
            if item.get("agent") in {portfolio_id, agent_value} and "trade_id" in item:
                trades.append(ExecutedTrade.model_validate(item))
        result[portfolio_id] = portfolio_metrics(
            snapshots,
            trades,
            initial_equity=Decimal(str(initial_equity)),
        )
    benchmark_rows = store.records("benchmark_snapshots")
    benchmark_initial = Decimal(str(settings.baselines["passive_benchmark"].initial_budget))
    if benchmark_rows:
        latest_benchmark = benchmark_rows[-1]
        current = Decimal(str(latest_benchmark["equity"]))
        result["passive_benchmark"] = {
            "initial_capital": float(benchmark_initial),
            "current_equity": float(current),
            "total_return": float((current - benchmark_initial) / benchmark_initial),
            "realized_pnl": 0.0,
            "unrealized_pnl": float(current - benchmark_initial),
            "max_drawdown": float(latest_benchmark.get("drawdown_pct", 0)),
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "trade_count": 0,
            "win_rate": None,
            "average_winner": None,
            "average_loser": None,
            "profit_factor": None,
            "exposure": 1.0,
            "turnover": 0.0,
        }
    else:
        result["passive_benchmark"] = portfolio_metrics([], [], initial_equity=benchmark_initial)
    decision_rows = _with_forward_returns(store.records("decisions"))
    result["codex_vs_quant"] = {
        agent: comparison_metrics([row for row in decision_rows if row.get("agent") == agent])
        for agent in ("aggressive", "conservative")
    }
    return result


def _with_forward_returns(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = [dict(row) for row in rows]
    for index, row in enumerate(enriched):
        next_row = next(
            (
                candidate
                for candidate in enriched[index + 1 :]
                if candidate.get("agent") == row.get("agent")
                and candidate.get("symbol") == row.get("symbol")
            ),
            None,
        )
        if next_row is None or not row.get("market_price"):
            continue
        current = float(row["market_price"])
        future = float(next_row["market_price"])
        raw_return = (future - current) / current
        action = row.get("codex_action")
        row["return"] = -raw_return if action == "SELL" else raw_return if action == "BUY" else 0.0
    return enriched


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))
