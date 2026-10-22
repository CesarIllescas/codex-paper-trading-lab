from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from config.settings import load_settings
from core.models import (
    AgentType,
    Candle,
    MarketSnapshot,
    PortfolioSnapshot,
    TradeAction,
    TradeIntent,
    hold_intent,
)
from observability.jsonl import JsonlDecisionLogger
from persistence.database import SQLiteStore
from portfolio.shadow import ShadowPortfolio
from risk import AggressiveRiskEngine
from runtime.health import HealthStatus
from runtime.orchestrator import AgentServices, RuntimeOrchestrator


class FakeBroker:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.submissions = 0
        self.snapshot = PortfolioSnapshot(
            portfolio_id="aggressive_codex",
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            cash=Decimal("10000"),
            equity=Decimal("10000"),
            buying_power=Decimal("10000"),
            peak_equity=Decimal("10000"),
            drawdown_pct=Decimal("0"),
        )

    def account_snapshot(self, portfolio_id: str, **kwargs: object) -> PortfolioSnapshot:
        del portfolio_id, kwargs
        self.events.append("reconcile")
        return self.snapshot

    def submit_approved(self, decision: object) -> None:
        del decision
        self.submissions += 1


class FakeStream:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self) -> asyncio.Task[None]:
        self.events.append("stream_start")

        async def done() -> None:
            return None

        return asyncio.create_task(done())

    async def wait_ready(self) -> None:
        self.events.append("stream_ready")

    async def stop(self) -> None:
        self.events.append("stream_stop")


class FakeServer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("codex_start")

    async def stop(self) -> None:
        self.events.append("codex_stop")


class StartupFailingServer(FakeServer):
    async def start(self) -> None:
        self.events.append("codex_start")
        raise RuntimeError("startup failed")


class FakePriceProvider:
    def get_snapshot(
        self,
        symbol: str,
        *,
        decision_timestamp: datetime,
        **kwargs: object,
    ) -> MarketSnapshot:
        del kwargs
        bars = []
        for index in range(55):
            close = Decimal("100") + index
            bars.append(
                Candle(
                    timestamp=decision_timestamp - timedelta(minutes=15 * (55 - index)),
                    open=close - 1,
                    high=close + 1,
                    low=close - 2,
                    close=close,
                    volume=Decimal("1000") + index,
                )
            )
        from market.indicators import calculate_indicators

        return MarketSnapshot(
            symbol=symbol,
            decision_timestamp=decision_timestamp,
            price=bars[-1].close,
            latest_bar_at=bars[-1].timestamp,
            candles=tuple(bars),
            indicators=calculate_indicators(bars),
        )


class FailingCodexAgent:
    async def create_intent(
        self,
        market: MarketSnapshot,
        signals: list,
        portfolio: PortfolioSnapshot,
        **kwargs: object,
    ) -> TradeIntent:
        del signals, portfolio, kwargs
        return hold_intent(
            AgentType.AGGRESSIVE,
            market.symbol,
            market.decision_timestamp,
            "Codex timeout",
            codex_valid=False,
        )


class BuyingCodexAgent:
    async def create_intent(
        self,
        market: MarketSnapshot,
        signals: list,
        portfolio: PortfolioSnapshot,
        **kwargs: object,
    ) -> TradeIntent:
        del signals, portfolio, kwargs
        return TradeIntent(
            agent=AgentType.AGGRESSIVE,
            action=TradeAction.BUY,
            symbol=market.symbol,
            confidence=0.9,
            strategy_bias="test",
            thesis="test buy",
            suggested_stop_loss_pct=0.02,
            decision_timestamp=market.decision_timestamp,
        )


def services(settings: object, events: list[str], trading_agent: object) -> AgentServices:
    broker = FakeBroker(events)
    return AgentServices(
        agent=AgentType.AGGRESSIVE,
        broker=broker,
        price_provider=FakePriceProvider(),
        codex_server=FakeServer(events),
        trading_agent=trading_agent,
        risk_engine=AggressiveRiskEngine(settings.agents["aggressive"].risk),  # type: ignore[attr-defined]
        shadow=ShadowPortfolio(AgentType.AGGRESSIVE_QUANT, Decimal("10000")),
        stream=FakeStream(events),
    )


@pytest.mark.asyncio
async def test_restart_startup_order_codex_failure_and_cycle_audit(tmp_path: Path) -> None:
    settings = load_settings(environ={"TRADING_ENV": "paper"})
    events: list[str] = []
    service = services(settings, events, FailingCodexAgent())
    store = SQLiteStore(":memory:")
    orchestrator = RuntimeOrchestrator(
        settings,
        store,
        JsonlDecisionLogger(tmp_path / "audit.jsonl"),
        [AgentType.AGGRESSIVE],
        services={AgentType.AGGRESSIVE: service},
    )
    await orchestrator.startup()
    assert events.index("reconcile") < events.index("stream_start") < events.index("codex_start")
    cycle_id = await orchestrator.run_cycle(
        AgentType.AGGRESSIVE,
        decision_timestamp=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    records = store.cycle_records(cycle_id)
    required = {
        "market_snapshots",
        "strategy_signals",
        "quant_decisions",
        "codex_decisions",
        "risk_decisions",
        "portfolio_snapshots",
        "decisions",
        "runtime_events",
    }
    assert required <= records.keys()
    assert all(not row["codex_valid"] for row in records["codex_decisions"])
    assert service.broker.submissions == 0
    assert any(row["event"] == "codex_failure" for row in records["runtime_events"])
    await orchestrator.shutdown()

    restart_events: list[str] = []
    restarted = RuntimeOrchestrator(
        settings,
        store,
        JsonlDecisionLogger(tmp_path / "restart.jsonl"),
        [AgentType.AGGRESSIVE],
        services={AgentType.AGGRESSIVE: services(settings, restart_events, FailingCodexAgent())},
    )
    await restarted.startup()
    assert restart_events[0] == "reconcile"
    assert restarted.health.get("aggressive_portfolio_reconciliation").status is (
        HealthStatus.HEALTHY
    )
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_unhealthy_dependency_blocks_paper_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENV", "paper")
    settings = load_settings(environ={"TRADING_ENV": "paper"})
    events: list[str] = []
    service = services(settings, events, BuyingCodexAgent())
    store = SQLiteStore(":memory:")
    orchestrator = RuntimeOrchestrator(
        settings,
        store,
        JsonlDecisionLogger(tmp_path / "audit.jsonl"),
        [AgentType.AGGRESSIVE],
        paper_execution=True,
        services={AgentType.AGGRESSIVE: service},
    )
    await orchestrator.startup()
    orchestrator.health.set("database", HealthStatus.UNHEALTHY, "forced test failure")
    await orchestrator.run_cycle(
        AgentType.AGGRESSIVE,
        decision_timestamp=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )
    assert service.broker.submissions == 0
    await orchestrator.shutdown()


@pytest.mark.asyncio
async def test_codex_startup_failure_stays_no_trade_until_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_ENV", "paper")
    settings = load_settings(environ={"TRADING_ENV": "paper"})
    events: list[str] = []
    service = services(settings, events, BuyingCodexAgent())
    service.codex_server = StartupFailingServer(events)
    store = SQLiteStore(":memory:")
    orchestrator = RuntimeOrchestrator(
        settings,
        store,
        JsonlDecisionLogger(tmp_path / "startup-failure.jsonl"),
        [AgentType.AGGRESSIVE],
        paper_execution=True,
        services={AgentType.AGGRESSIVE: service},
    )

    await orchestrator.startup()
    await orchestrator.run_cycle(
        AgentType.AGGRESSIVE,
        decision_timestamp=datetime(2026, 1, 2, 12, tzinfo=UTC),
    )

    assert not service.codex_startup_ready
    assert service.broker.submissions == 0
    assert all(not decision["codex_valid"] for decision in store.records("codex_decisions"))
    startup_health = orchestrator.health.get("aggressive_codex_app_server")
    assert startup_health is not None
    assert startup_health.message == "Codex startup failed: RuntimeError"
    await orchestrator.shutdown()
