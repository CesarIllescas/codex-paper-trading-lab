from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import AggressiveAgent, ConservativeAgent
from brokers.alpaca import AlpacaBrokerAdapter, build_alpaca_broker
from brokers.alpaca_stream import AlpacaOrderStream, build_alpaca_order_stream
from config.settings import Settings
from core.models import (
    AgentType,
    ExecutedTrade,
    OrderStatus,
    PortfolioSnapshot,
    TradeAction,
    TradeUpdate,
    hold_intent,
)
from intelligence.codex_app_server import CodexAppServer, CodexAppServerError
from intelligence.codex_client import CodexClient
from intelligence.codex_threads import CodexThreadManager
from market.prices import AlpacaPriceProvider, build_alpaca_price_provider
from observability.jsonl import JsonlDecisionLogger
from persistence.database import SQLiteStore
from portfolio.shadow import ShadowPortfolio
from risk import AggressiveRiskEngine, ConservativeRiskEngine
from risk.kill_switch import KillSwitchState
from runtime.health import HealthRegistry, HealthStatus
from runtime.reconciliation import ReconciliationService
from runtime.scheduler import CandleScheduler
from strategies.baseline import deterministic_baseline
from strategies.pipeline import generate_signals, strategy_weights


@dataclass(slots=True)
class AgentServices:
    agent: AgentType
    broker: Any
    price_provider: Any
    codex_server: Any
    trading_agent: Any
    risk_engine: Any
    shadow: ShadowPortfolio
    thread_manager: CodexThreadManager | None = None
    stream: Any | None = None
    reconciler: ReconciliationService | None = None
    codex_startup_ready: bool = True


class RuntimeOrchestrator:
    """Own restart-safe services and execute fail-closed autonomous cycles."""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        logger: JsonlDecisionLogger,
        agents: list[AgentType],
        *,
        paper_execution: bool = False,
        services: dict[AgentType, AgentServices] | None = None,
    ) -> None:
        production = {AgentType.AGGRESSIVE, AgentType.CONSERVATIVE}
        if any(agent not in production for agent in agents):
            raise ValueError("runtime accepts production agents only")
        if settings.broker.paper is not True or settings.app.environment != "paper":
            raise RuntimeError("live execution is impossible")
        if paper_execution and os.environ.get("TRADING_ENV") != "paper":
            raise RuntimeError("explicit paper execution requires TRADING_ENV=paper")
        self.settings = settings
        self.store = store
        self.logger = logger
        self.agents = agents
        self.paper_execution = paper_execution
        self.health = HealthRegistry(store)
        self.scheduler = CandleScheduler(settings.runtime.scheduler.candle_grace_seconds)
        self.services = services or {}
        self._stop = asyncio.Event()
        self._started = False

    async def startup(self) -> None:
        database_ok = self.store.ping()
        self.health.set(
            "database",
            HealthStatus.HEALTHY if database_ok else HealthStatus.UNHEALTHY,
            "SQLite available" if database_ok else "SQLite unavailable",
        )
        self.health.set("kill_switch", HealthStatus.HEALTHY, "deterministic kill switch enabled")
        if not database_ok:
            raise RuntimeError("database health check failed")
        for agent in self.agents:
            service = self.services.get(agent) or self._build_services(agent)
            self.services[agent] = service
            service.reconciler = ReconciliationService(
                self.store, self.health, agent, service.broker
            )
            result = service.reconciler.reconcile()
            if not result.healthy:
                self.store.record(
                    "runtime_events",
                    {"agent": agent.value, "event": "startup_reconciliation_failed"},
                )
            if service.stream is None:
                service.stream = self._build_stream(service)
            service.stream.start()
            try:
                await service.stream.wait_ready()
                self.health.set(
                    f"{agent.value}_trade_stream",
                    HealthStatus.HEALTHY,
                    "trade update stream connected",
                    agent=agent,
                )
            except Exception as exc:
                self.health.set(
                    f"{agent.value}_trade_stream",
                    HealthStatus.UNHEALTHY,
                    f"stream unavailable at startup: {type(exc).__name__}",
                    agent=agent,
                )
            try:
                service.codex_startup_ready = False
                await service.codex_server.start()
                self.health.set(
                    f"{agent.value}_codex_app_server",
                    HealthStatus.HEALTHY,
                    "Codex App Server initialized",
                    agent=agent,
                )
                if service.thread_manager is not None:
                    await service.thread_manager.get_or_create(agent, service.codex_server)
                self.health.set(
                    f"{agent.value}_codex_thread",
                    HealthStatus.HEALTHY,
                    "persistent Codex thread resumed",
                    agent=agent,
                )
                service.codex_startup_ready = True
            except Exception as exc:
                await service.codex_server.stop()
                detail = str(exc) if isinstance(exc, CodexAppServerError) else type(exc).__name__
                self.health.set(
                    f"{agent.value}_codex_app_server",
                    HealthStatus.UNHEALTHY,
                    f"Codex startup failed: {detail}",
                    agent=agent,
                )
                self.health.set(
                    f"{agent.value}_codex_thread",
                    HealthStatus.UNHEALTHY,
                    "Codex thread could not be resumed",
                    agent=agent,
                )
        self._started = True

    async def run_cycle(
        self,
        agent: AgentType,
        *,
        decision_timestamp: datetime | None = None,
    ) -> str:
        if not self._started:
            raise RuntimeError("orchestrator must complete startup first")
        cycle_id = str(uuid4())
        at = (decision_timestamp or datetime.now(UTC)).astimezone(UTC)
        service = self.services[agent]
        self.store.record(
            "runtime_events",
            {"event": "decision_cycle_started", "agent": agent.value, "timestamp": at},
            decision_cycle_id=cycle_id,
        )
        try:
            assert service.reconciler is not None
            reconciliation = service.reconciler.reconcile(decision_cycle_id=cycle_id)
            portfolio = reconciliation.broker_snapshot
            universe = getattr(self.settings.market, agent.value)
            markets = {}
            for symbol in universe.universe:
                market = service.price_provider.get_snapshot(
                    symbol,
                    timeframe=universe.timeframe,
                    stale_after_seconds=self.settings.market.stale_data_seconds,
                    decision_timestamp=at,
                )
                markets[symbol] = market
                self.store.record("market_snapshots", market, decision_cycle_id=cycle_id)
            market_healthy = bool(markets) and all(not item.stale for item in markets.values())
            self.health.set(
                f"{agent.value}_market_data",
                HealthStatus.HEALTHY if market_healthy else HealthStatus.UNHEALTHY,
                "latest completed bars are fresh" if market_healthy else "market data is stale",
                agent=agent,
            )
            prices = {symbol: item.price for symbol, item in markets.items()}
            shadow_snapshot = service.shadow.snapshot(prices, at)
            weights = strategy_weights(self.settings, agent)

            for symbol, market in markets.items():
                if (
                    agent is AgentType.CONSERVATIVE
                    and symbol == self.settings.baselines["passive_benchmark"].symbol
                ):
                    self._record_benchmark(market, cycle_id)
                signals = generate_signals(self.settings, agent, market)
                for signal in signals:
                    self.store.record("strategy_signals", signal, decision_cycle_id=cycle_id)
                quant_agent = service.shadow.portfolio_id
                quant_intent = deterministic_baseline(quant_agent, signals, weights)
                self.store.record("quant_decisions", quant_intent, decision_cycle_id=cycle_id)
                quant_duplicate = self.store.has_recent_order(
                    quant_agent.value,
                    symbol,
                    quant_intent.action.value,
                    self.settings.execution.duplicate_order_window_seconds,
                    at,
                )
                quant_risk = service.risk_engine.evaluate(
                    quant_intent,
                    shadow_snapshot,
                    market,
                    KillSwitchState(duplicate_order=quant_duplicate),
                )
                self.store.record(
                    "risk_decisions",
                    {"path": "quant", **quant_risk.model_dump(mode="json")},
                    decision_cycle_id=cycle_id,
                )
                shadow_trade = None
                if quant_risk.approved:
                    assert quant_risk.order_request is not None
                    self.store.record(
                        "orders",
                        {
                            "path": "quant",
                            **quant_risk.order_request.model_dump(mode="json"),
                            "status": OrderStatus.SUBMITTED.value,
                        },
                        decision_cycle_id=cycle_id,
                    )
                    shadow_trade = service.shadow.execute(quant_risk, market.price, at)
                    self.store.record("fills", shadow_trade, decision_cycle_id=cycle_id)
                    shadow_snapshot = service.shadow.snapshot(prices, at)

                codex_intent = await self._codex_intent(
                    service, market, signals, portfolio, cycle_id
                )
                self.store.record("codex_decisions", codex_intent, decision_cycle_id=cycle_id)
                blockers = self.health.execution_blockers(
                    agent, require_stream=self.paper_execution
                )
                duplicate = self.store.has_recent_order(
                    agent.value,
                    symbol,
                    codex_intent.action.value,
                    self.settings.execution.duplicate_order_window_seconds,
                    at,
                )
                codex_risk = service.risk_engine.evaluate(
                    codex_intent,
                    portfolio,
                    market,
                    KillSwitchState(
                        duplicate_order=duplicate,
                        portfolio_state_valid=reconciliation.healthy,
                        broker_state_valid=reconciliation.healthy,
                        extra_reasons=blockers,
                    ),
                )
                self.store.record(
                    "risk_decisions",
                    {"path": "codex", **codex_risk.model_dump(mode="json")},
                    decision_cycle_id=cycle_id,
                )
                broker_state = None
                if (
                    self.paper_execution
                    and codex_risk.approved
                    and self.health.order_execution_allowed(agent, require_stream=True)
                ):
                    assert codex_risk.order_request is not None
                    self.store.record(
                        "orders",
                        {
                            "path": "codex",
                            **codex_risk.order_request.model_dump(mode="json"),
                            "status": OrderStatus.SUBMITTED.value,
                        },
                        decision_cycle_id=cycle_id,
                    )
                    try:
                        broker_state = service.broker.submit_approved(codex_risk)
                        self.store.record(
                            "orders",
                            {
                                "path": "codex",
                                "agent": agent.value,
                                "action": codex_intent.action.value,
                                **asdict(broker_state),
                            },
                            decision_cycle_id=cycle_id,
                        )
                    except Exception as exc:
                        self.health.set(
                            f"{agent.value}_alpaca",
                            HealthStatus.UNHEALTHY,
                            f"order submission failed: {type(exc).__name__}",
                            agent=agent,
                        )
                        self.store.record(
                            "orders",
                            {
                                "path": "codex",
                                "agent": agent.value,
                                "symbol": symbol,
                                "action": codex_intent.action.value,
                                "status": OrderStatus.REJECTED.value,
                                "error_type": type(exc).__name__,
                            },
                            decision_cycle_id=cycle_id,
                        )

                decision_record = {
                    "timestamp": at.isoformat(),
                    "agent": agent.value,
                    "symbol": symbol,
                    "market_snapshot_id": market.snapshot_id,
                    "market_price": str(market.price),
                    "strategy_signals": [item.model_dump(mode="json") for item in signals],
                    "codex_action": codex_intent.action.value,
                    "quant_action": quant_intent.action.value,
                    "confidence": codex_intent.confidence,
                    "codex_valid": codex_intent.codex_valid,
                    "codex_rejected": not codex_risk.approved
                    and codex_intent.action is not TradeAction.HOLD,
                    "codex_risk": codex_risk.model_dump(mode="json"),
                    "quant_risk": quant_risk.model_dump(mode="json"),
                    "broker_status": None if broker_state is None else broker_state.status.value,
                    "shadow_fill": None
                    if shadow_trade is None
                    else shadow_trade.model_dump(mode="json"),
                    "dry_run": not self.paper_execution,
                    "return": None,
                }
                self.store.record("decisions", decision_record, decision_cycle_id=cycle_id)
                self.logger.log(
                    "trading_decision", {"decision_cycle_id": cycle_id, **decision_record}
                )

            self.store.record("portfolio_snapshots", shadow_snapshot, decision_cycle_id=cycle_id)
            self.store.record(
                "equity_history",
                {
                    "portfolio_id": shadow_snapshot.portfolio_id,
                    "timestamp": shadow_snapshot.timestamp,
                    "equity": shadow_snapshot.equity,
                },
                decision_cycle_id=cycle_id,
            )
            self.health.set(
                f"{agent.value}_last_cycle",
                HealthStatus.HEALTHY,
                f"completed cycle {cycle_id}",
                critical=False,
                agent=agent,
            )
            self.store.record(
                "runtime_events",
                {"event": "decision_cycle_completed", "agent": agent.value},
                decision_cycle_id=cycle_id,
            )
            from application import metrics_report

            current_metrics = metrics_report(self.store, self.settings)
            self.store.record(
                "performance_metrics",
                {
                    "agent": agent.value,
                    "portfolios": {
                        key: value
                        for key, value in current_metrics.items()
                        if key.startswith(agent.value) or key == "passive_benchmark"
                    },
                    "codex_vs_quant": current_metrics["codex_vs_quant"][agent.value],
                },
                decision_cycle_id=cycle_id,
            )
        except Exception as exc:
            self.health.set(
                f"{agent.value}_last_cycle",
                HealthStatus.UNHEALTHY,
                f"cycle failed closed: {type(exc).__name__}",
                agent=agent,
            )
            self.store.record(
                "runtime_events",
                {
                    "event": "decision_cycle_failed_closed",
                    "agent": agent.value,
                    "error_type": type(exc).__name__,
                },
                decision_cycle_id=cycle_id,
            )
            self.store.record(
                "decisions",
                {
                    "timestamp": at.isoformat(),
                    "agent": agent.value,
                    "symbol": None,
                    "codex_action": TradeAction.HOLD.value,
                    "quant_action": TradeAction.HOLD.value,
                    "confidence": 0,
                    "codex_valid": False,
                    "codex_rejected": True,
                    "failure_reason": type(exc).__name__,
                    "dry_run": not self.paper_execution,
                    "return": None,
                },
                decision_cycle_id=cycle_id,
            )
            self.logger.log(
                "decision_cycle_failed_closed",
                {
                    "decision_cycle_id": cycle_id,
                    "agent": agent.value,
                    "error_type": type(exc).__name__,
                },
            )
        return cycle_id

    async def run_forever(self) -> None:
        tasks: list[asyncio.Task[Any]] = []
        stop_task: asyncio.Task[bool] | None = None
        try:
            await self.startup()
            tasks = [asyncio.create_task(self._run_schedule(agent)) for agent in self.agents]
            tasks.append(asyncio.create_task(self._periodic_reconciliation()))
            tasks.append(asyncio.create_task(self._periodic_health()))
            stop_task = asyncio.create_task(self._stop.wait())
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            failed = next((task for task in done if task is not stop_task), None)
            if failed is not None:
                await failed
        finally:
            for task in tasks:
                task.cancel()
            if stop_task is not None:
                stop_task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if stop_task is not None:
                await asyncio.gather(stop_task, return_exceptions=True)
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        for service in self.services.values():
            if service.stream is not None:
                await service.stream.stop()
            await service.codex_server.stop()
        self._started = False

    async def handle_trade_update(self, service: AgentServices, update: TradeUpdate) -> None:
        cycle_id = self._order_cycle(update.client_order_id, update.order_id)
        self.store.record("order_updates", update, decision_cycle_id=cycle_id)
        if (
            update.fill_delta_quantity > 0
            and update.fill_price is not None
            and update.action is not None
        ):
            fill = ExecutedTrade(
                trade_id=update.event_id,
                order_id=update.order_id,
                agent=service.agent,
                symbol=update.symbol,
                action=update.action,
                quantity=update.fill_delta_quantity,
                price=update.fill_price,
                executed_at=update.timestamp,
                status=update.status,
            )
            self.store.record("fills", fill, decision_cycle_id=cycle_id)
        if update.status in {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }:
            assert service.reconciler is not None
            service.reconciler.reconcile(decision_cycle_id=cycle_id)

    async def _codex_intent(
        self,
        service: AgentServices,
        market: Any,
        signals: list[Any],
        portfolio: PortfolioSnapshot,
        cycle_id: str,
    ) -> Any:
        if not service.codex_startup_ready:
            reason = "Codex startup failed; restart required before another decision"
            self._record_codex_failure(
                service.agent,
                cycle_id,
                reason,
                update_health=False,
            )
            return hold_intent(
                service.agent,
                market.symbol,
                market.decision_timestamp,
                reason,
                codex_valid=False,
            )
        try:
            recent = self.store.records("decisions")[-5:]
            intent = await service.trading_agent.create_intent(
                market, signals, portfolio, recent_decisions=recent
            )
            if intent.codex_valid:
                self.health.set(
                    f"{service.agent.value}_codex_app_server",
                    HealthStatus.HEALTHY,
                    "Codex decision completed",
                    agent=service.agent,
                )
                self.health.set(
                    f"{service.agent.value}_codex_thread",
                    HealthStatus.HEALTHY,
                    "Codex thread responsive",
                    agent=service.agent,
                )
            else:
                self._record_codex_failure(service.agent, cycle_id, "invalid or unavailable")
            return intent
        except Exception as exc:
            self._record_codex_failure(service.agent, cycle_id, type(exc).__name__)
            return hold_intent(
                service.agent,
                market.symbol,
                market.decision_timestamp,
                "Codex failure during scheduled cycle",
                codex_valid=False,
            )

    def _record_codex_failure(
        self,
        agent: AgentType,
        cycle_id: str,
        reason: str,
        *,
        update_health: bool = True,
    ) -> None:
        if update_health:
            self.health.set(
                f"{agent.value}_codex_app_server",
                HealthStatus.UNHEALTHY,
                f"Codex failure: {reason}",
                agent=agent,
            )
            self.health.set(
                f"{agent.value}_codex_thread",
                HealthStatus.UNHEALTHY,
                "Codex decision unavailable; execution paused",
                agent=agent,
            )
        self.store.record(
            "runtime_events",
            {"event": "codex_failure", "agent": agent.value, "reason": reason},
            decision_cycle_id=cycle_id,
        )

    async def _run_schedule(self, agent: AgentType) -> None:
        async def callback(boundary: datetime) -> None:
            await self.run_cycle(agent, decision_timestamp=boundary)

        def next_boundary() -> datetime:
            now = datetime.now(UTC)
            if agent is AgentType.AGGRESSIVE:
                return self.scheduler.next_boundary(agent, now)
            sessions = self.services[agent].broker.calendar_sessions(now, now + timedelta(days=14))
            return self.scheduler.next_boundary(agent, now, sessions)

        self.health.set(
            f"{agent.value}_scheduler",
            HealthStatus.HEALTHY,
            "completed-candle scheduler active",
            critical=False,
            agent=agent,
        )
        try:
            await self.scheduler.run(next_boundary, callback, self._stop)
        except Exception as exc:
            self.health.set(
                f"{agent.value}_scheduler",
                HealthStatus.UNHEALTHY,
                f"scheduler stopped: {type(exc).__name__}",
                agent=agent,
            )
            self.store.record(
                "runtime_events",
                {
                    "event": "scheduler_failed_closed",
                    "agent": agent.value,
                    "error_type": type(exc).__name__,
                },
            )
            raise

    async def _periodic_reconciliation(self) -> None:
        interval = self.settings.runtime.scheduler.reconciliation_interval_seconds
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                return
            except TimeoutError:
                for service in self.services.values():
                    assert service.reconciler is not None
                    try:
                        service.reconciler.reconcile()
                    except Exception:
                        pass

    async def _periodic_health(self) -> None:
        interval = self.settings.runtime.scheduler.health_interval_seconds
        while not self._stop.is_set():
            database_ok = self.store.ping()
            self.health.set(
                "database",
                HealthStatus.HEALTHY if database_ok else HealthStatus.UNHEALTHY,
                "SQLite available" if database_ok else "SQLite unavailable",
            )
            for service in self.services.values():
                server_running = bool(getattr(service.codex_server, "running", True))
                self.health.set(
                    f"{service.agent.value}_codex_app_server",
                    HealthStatus.HEALTHY if server_running else HealthStatus.UNHEALTHY,
                    "Codex App Server running"
                    if server_running
                    else "Codex App Server process unavailable",
                    agent=service.agent,
                )
                stream_ready = bool(getattr(service.stream, "ready", True))
                self.health.set(
                    f"{service.agent.value}_trade_stream",
                    HealthStatus.HEALTHY if stream_ready else HealthStatus.UNHEALTHY,
                    "trade update stream connected"
                    if stream_ready
                    else "trade update stream unavailable",
                    agent=service.agent,
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    def _build_services(self, agent: AgentType) -> AgentServices:
        key, secret = _credentials(agent)
        broker: AlpacaBrokerAdapter = build_alpaca_broker(agent)
        provider: AlpacaPriceProvider = build_alpaca_price_provider(key, secret)
        runtime = self.settings.codex.runtime
        server = CodexAppServer(
            os.environ.get("CODEX_EXECUTABLE", runtime.executable),
            Path(runtime.workspace_root) / agent.value,
            startup_timeout=runtime.startup_timeout_seconds,
            turn_timeout=runtime.turn_timeout_seconds,
        )
        threads = CodexThreadManager(self.store, strict_resume=True)
        client = CodexClient(
            agent,
            server,
            threads,
            self.settings.codex.threads[agent.value].prompt_file,
        )
        agent_settings = self.settings.agents[agent.value]
        trading_agent = (
            AggressiveAgent(client, agent_settings.decision.minimum_codex_confidence)
            if agent is AgentType.AGGRESSIVE
            else ConservativeAgent(client, agent_settings.decision.minimum_codex_confidence)
        )
        risk = (
            AggressiveRiskEngine(agent_settings.risk)
            if agent is AgentType.AGGRESSIVE
            else ConservativeRiskEngine(agent_settings.risk)
        )
        quant_agent = (
            AgentType.AGGRESSIVE_QUANT
            if agent is AgentType.AGGRESSIVE
            else AgentType.CONSERVATIVE_QUANT
        )
        baseline = self.settings.baselines[quant_agent.value]
        raw = self.store.latest_portfolio_snapshot(quant_agent.value)
        shadow = (
            ShadowPortfolio(quant_agent, Decimal(str(baseline.initial_budget)))
            if raw is None
            else ShadowPortfolio.from_snapshot(
                quant_agent,
                Decimal(str(baseline.initial_budget)),
                PortfolioSnapshot.model_validate(raw),
            )
        )
        return AgentServices(
            agent=agent,
            broker=broker,
            price_provider=provider,
            codex_server=server,
            trading_agent=trading_agent,
            risk_engine=risk,
            shadow=shadow,
            thread_manager=threads,
        )

    def _build_stream(self, service: AgentServices) -> AlpacaOrderStream:
        async def update_handler(update: TradeUpdate) -> None:
            await self.handle_trade_update(service, update)

        async def state_handler(healthy: bool, message: str) -> None:
            self.health.set(
                f"{service.agent.value}_trade_stream",
                HealthStatus.HEALTHY if healthy else HealthStatus.UNHEALTHY,
                message,
                agent=service.agent,
            )
            if healthy and service.reconciler is not None:
                try:
                    service.reconciler.reconcile()
                except Exception:
                    pass

        stream = self.settings.runtime.stream
        return build_alpaca_order_stream(
            service.agent,
            update_handler,
            state_handler,
            max_reconnect_attempts=stream.max_reconnect_attempts,
            reconnect_base_seconds=stream.reconnect_base_seconds,
            reconnect_max_seconds=stream.reconnect_max_seconds,
            startup_timeout_seconds=stream.startup_timeout_seconds,
        )

    def _order_cycle(self, client_order_id: str, order_id: str) -> str | None:
        for row in reversed(self.store.records_with_metadata("orders")):
            if row.get("client_order_id") == client_order_id or row.get("order_id") == order_id:
                return row.get("_decision_cycle_id")
        return None

    def _record_benchmark(self, market: Any, cycle_id: str) -> None:
        rows = [
            row
            for row in self.store.records("benchmark_snapshots")
            if row.get("source") == "runtime_spy"
        ]
        initial = Decimal(str(self.settings.baselines["passive_benchmark"].initial_budget))
        first_price = market.price if not rows else Decimal(str(rows[0]["first_price"]))
        equity = initial * market.price / first_price
        previous_peak = max([Decimal(str(row["equity"])) for row in rows] or [initial])
        peak = max(previous_peak, equity)
        self.store.record(
            "benchmark_snapshots",
            {
                "portfolio_id": "passive_benchmark",
                "source": "runtime_spy",
                "timestamp": market.decision_timestamp,
                "symbol": market.symbol,
                "first_price": first_price,
                "price": market.price,
                "equity": equity,
                "drawdown_pct": (peak - equity) / peak,
            },
            decision_cycle_id=cycle_id,
        )


def _credentials(agent: AgentType) -> tuple[str, str]:
    prefix = "ALPACA_AGGRESSIVE" if agent is AgentType.AGGRESSIVE else "ALPACA_CONSERVATIVE"
    key = os.environ.get(f"{prefix}_API_KEY")
    secret = os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"missing Alpaca paper credentials for {agent.value}")
    return key, secret
