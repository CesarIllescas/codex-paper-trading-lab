from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from brokers.alpaca import BrokerOrderState
from brokers.alpaca_stream import AlpacaOrderStream, normalize_trade_update
from core.models import AgentType, OrderStatus, PortfolioSnapshot, Position, TradeUpdate
from persistence.database import SQLiteStore
from runtime.health import HealthRegistry, HealthStatus
from runtime.reconciliation import ReconciliationService


def raw_update(event: str, filled: str = "0", quantity: str | None = None) -> dict:
    result = {
        "event": event,
        "timestamp": "2026-01-01T12:00:00Z",
        "order": {
            "id": "order-1",
            "client_order_id": "client-1",
            "symbol": "BTC/USD",
            "side": "buy",
            "filled_qty": filled,
            "filled_avg_price": "100" if filled != "0" else None,
        },
    }
    if quantity is not None:
        result["qty"] = quantity
        result["price"] = "100"
    return result


def test_trade_stream_normalizes_partial_fills_and_rejections() -> None:
    partial = normalize_trade_update(AgentType.AGGRESSIVE, raw_update("partial_fill", "2", "2"))
    rejected = normalize_trade_update(AgentType.AGGRESSIVE, raw_update("rejected"))
    assert partial.status.value == "partially_filled"
    assert partial.fill_delta_quantity == Decimal("2")
    assert rejected.status.value == "rejected"
    assert rejected.fill_delta_quantity == 0


def test_trade_stream_normalizes_alpaca_side_enum() -> None:
    from alpaca.trading.enums import OrderSide

    update = raw_update("fill", "1", "1")
    update["order"]["side"] = OrderSide.BUY
    normalized = normalize_trade_update(AgentType.AGGRESSIVE, update)
    assert normalized.action.value == "BUY"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("submitted", "submitted"),
        ("accepted", "accepted"),
        ("fill", "filled"),
        ("canceled", "cancelled"),
    ],
)
def test_trade_stream_preserves_order_lifecycle(event: str, expected: str) -> None:
    filled = "1" if event == "fill" else "0"
    assert (
        normalize_trade_update(AgentType.AGGRESSIVE, raw_update(event, filled)).status.value
        == expected
    )


class FakeConnection:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.connected = False
        self.handler = None
        self.stopped = asyncio.Event()

    def subscribe_trade_updates(self, handler: object) -> None:
        self.handler = handler

    async def run(self) -> None:
        self.connected = True
        await asyncio.sleep(0.02)
        if self.fail:
            self.connected = False
            raise ConnectionError
        await self.stopped.wait()

    def stop(self) -> None:
        self.stopped.set()


@pytest.mark.asyncio
async def test_trade_stream_reconnect_is_bounded_and_recovers() -> None:
    connections: list[FakeConnection] = []
    states: list[bool] = []

    def factory() -> FakeConnection:
        connection = FakeConnection(fail=not connections)
        connections.append(connection)
        return connection

    async def update_handler(update: TradeUpdate) -> None:
        del update

    async def state_handler(healthy: bool, message: str) -> None:
        del message
        states.append(healthy)

    stream = AlpacaOrderStream(
        AgentType.AGGRESSIVE,
        factory,
        update_handler,
        state_handler,
        max_reconnect_attempts=2,
        reconnect_base_seconds=0.001,
        startup_timeout_seconds=1,
    )
    stream.start()
    await stream.wait_ready()
    for _ in range(100):
        if len(connections) == 2 and stream.ready:
            break
        await asyncio.sleep(0.005)
    assert len(connections) == 2
    assert True in states and False in states
    await stream.stop()


@pytest.mark.asyncio
async def test_blocking_trade_stream_dispatches_updates_on_owner_thread() -> None:
    owner_thread_id = threading.get_ident()
    callback_thread_ids: list[int] = []
    store = SQLiteStore(":memory:")

    class BlockingConnection:
        def __init__(self) -> None:
            self.connected = False
            self.handler = None
            self.stopped = threading.Event()
            self.update_complete = threading.Event()

        def subscribe_trade_updates(self, handler: object) -> None:
            self.handler = handler

        def run(self) -> None:
            self.connected = True
            assert self.handler is not None
            asyncio.run(self.handler(raw_update("accepted")))  # type: ignore[operator]
            self.update_complete.set()
            self.stopped.wait(timeout=2)

        def stop(self) -> None:
            self.stopped.set()

    connection = BlockingConnection()

    async def update_handler(update: TradeUpdate) -> None:
        callback_thread_ids.append(threading.get_ident())
        store.record("order_updates", update)

    async def state_handler(healthy: bool, message: str) -> None:
        del healthy, message

    stream = AlpacaOrderStream(
        AgentType.AGGRESSIVE,
        lambda: connection,
        update_handler,
        state_handler,
        startup_timeout_seconds=1,
    )
    stream.start()
    await stream.wait_ready()
    for _ in range(100):
        if connection.update_complete.is_set():
            break
        await asyncio.sleep(0.005)

    assert connection.update_complete.is_set()
    assert callback_thread_ids == [owner_thread_id]
    assert store.latest_record("order_updates")["status"] == "accepted"
    await stream.stop()
    store.close()


@pytest.mark.asyncio
async def test_blocking_trade_stream_shutdown_is_bounded() -> None:
    class StuckConnection:
        def __init__(self) -> None:
            self.connected = False
            self.handler = None
            self.forever = threading.Event()

        def subscribe_trade_updates(self, handler: object) -> None:
            self.handler = handler

        def run(self) -> None:
            self.connected = True
            self.forever.wait()

        def stop(self) -> None:
            self.forever.wait()

    async def update_handler(update: TradeUpdate) -> None:
        del update

    async def state_handler(healthy: bool, message: str) -> None:
        del healthy, message

    stream = AlpacaOrderStream(
        AgentType.CONSERVATIVE,
        StuckConnection,
        update_handler,
        state_handler,
        startup_timeout_seconds=1,
        shutdown_timeout_seconds=0.01,
    )
    stream.start()
    await stream.wait_ready()
    await asyncio.wait_for(stream.stop(), timeout=0.2)


class SnapshotBroker:
    def __init__(self, snapshot: PortfolioSnapshot) -> None:
        self.snapshot = snapshot

    def account_snapshot(self, portfolio_id: str, **kwargs: object) -> PortfolioSnapshot:
        del portfolio_id, kwargs
        return self.snapshot


class RecoveringSnapshotBroker(SnapshotBroker):
    def get_order_status(self, order_id: str) -> BrokerOrderState:
        return BrokerOrderState(
            order_id=order_id,
            client_order_id="client-1",
            symbol="BTC/USD",
            status=OrderStatus.FILLED,
            requested_quantity=Decimal("1"),
            filled_quantity=Decimal("1"),
            average_fill_price=Decimal("100"),
        )


def portfolio(at: datetime, cash: str = "1000", with_position: bool = False) -> PortfolioSnapshot:
    positions = ()
    if with_position:
        positions = (
            Position(
                symbol="BTC/USD",
                quantity=Decimal("1"),
                average_entry_price=Decimal("100"),
                current_price=Decimal("100"),
                market_value=Decimal("100"),
            ),
        )
    return PortfolioSnapshot(
        portfolio_id="aggressive_codex",
        timestamp=at,
        cash=Decimal(cash),
        equity=Decimal("1000"),
        buying_power=Decimal(cash),
        positions=positions,
        peak_equity=Decimal("1000"),
        drawdown_pct=Decimal("0"),
        exposure=Decimal("100") if with_position else Decimal("0"),
    )


def test_startup_and_fill_explained_reconciliation() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLiteStore(":memory:")
    health = HealthRegistry(store)
    broker = SnapshotBroker(portfolio(at))
    service = ReconciliationService(store, health, AgentType.AGGRESSIVE, broker)
    assert service.reconcile().healthy

    broker.snapshot = portfolio(at + timedelta(minutes=1), "900", True)
    unexplained = service.reconcile()
    assert not unexplained.healthy
    assert health.get("aggressive_portfolio_reconciliation").status is HealthStatus.UNHEALTHY

    store.record(
        "order_updates",
        TradeUpdate(
            agent=AgentType.AGGRESSIVE,
            order_id="order-1",
            client_order_id="client-1",
            symbol="BTC/USD",
            action="BUY",
            status="partially_filled",
            cumulative_filled_quantity=Decimal("1"),
            fill_delta_quantity=Decimal("1"),
            fill_price=Decimal("100"),
            timestamp=at + timedelta(seconds=1),
        ),
    )
    explained = service.reconcile()
    assert explained.healthy and explained.explained
    assert store.latest_portfolio_snapshot("aggressive_codex")["cash"] == "900"
    assert store.latest_portfolio_snapshot("aggressive_codex")["positions"][0]["quantity"] == "1"


def test_reconciliation_recovers_missed_broker_fill_under_original_cycle() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLiteStore(":memory:")
    health = HealthRegistry(store)
    broker = RecoveringSnapshotBroker(portfolio(at))
    service = ReconciliationService(store, health, AgentType.AGGRESSIVE, broker)
    assert service.reconcile().healthy
    store.record(
        "orders",
        {
            "path": "codex",
            "agent": "aggressive",
            "action": "BUY",
            "order_id": "order-1",
            "client_order_id": "client-1",
            "symbol": "BTC/USD",
            "status": "submitted",
        },
        decision_cycle_id="cycle-1",
    )
    broker.snapshot = portfolio(at + timedelta(minutes=1), "900", True)

    result = service.reconcile()

    assert result.healthy and result.explained
    update = store.records_with_metadata("order_updates")[-1]
    fill = store.records_with_metadata("fills")[-1]
    assert update["status"] == "filled"
    assert update["_decision_cycle_id"] == "cycle-1"
    assert fill["quantity"] == "1"
    assert fill["_decision_cycle_id"] == "cycle-1"


def test_reconciliation_backfills_fill_when_stream_side_was_missing() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    store = SQLiteStore(":memory:")
    health = HealthRegistry(store)
    broker = SnapshotBroker(portfolio(at))
    service = ReconciliationService(store, health, AgentType.AGGRESSIVE, broker)
    assert service.reconcile().healthy
    store.record(
        "orders",
        {
            "path": "codex",
            "agent": "aggressive",
            "action": "BUY",
            "order_id": "order-1",
            "client_order_id": "client-1",
            "symbol": "BTC/USD",
            "status": "submitted",
        },
        decision_cycle_id="cycle-1",
    )
    store.record(
        "order_updates",
        TradeUpdate(
            agent=AgentType.AGGRESSIVE,
            order_id="order-1",
            client_order_id="client-1",
            symbol="BTC/USD",
            action=None,
            status=OrderStatus.FILLED,
            cumulative_filled_quantity=Decimal("1"),
            fill_delta_quantity=Decimal("1"),
            fill_price=Decimal("100"),
            timestamp=at + timedelta(seconds=1),
        ),
        decision_cycle_id="cycle-1",
    )

    service.reconcile()

    fill = store.records_with_metadata("fills")[-1]
    assert fill["action"] == "BUY"
    assert fill["quantity"] == "1"
    assert fill["_decision_cycle_id"] == "cycle-1"
