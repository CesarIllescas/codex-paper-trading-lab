from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from core.models import (
    AgentType,
    ExecutedTrade,
    OrderStatus,
    PortfolioSnapshot,
    ReconciliationResult,
    TradeAction,
    TradeUpdate,
)
from persistence.database import SQLiteStore
from runtime.health import HealthRegistry, HealthStatus


class ReconciliationBroker(Protocol):
    def account_snapshot(
        self,
        portfolio_id: str,
        *,
        peak_equity: Decimal | None = None,
        expected_positions: tuple | None = None,
        realized_pnl: Decimal = Decimal("0"),
    ) -> PortfolioSnapshot: ...

    def get_order_status(self, order_id: str) -> Any: ...


class ReconciliationService:
    def __init__(
        self,
        store: SQLiteStore,
        health: HealthRegistry,
        agent: AgentType,
        broker: ReconciliationBroker,
    ) -> None:
        if agent not in {AgentType.AGGRESSIVE, AgentType.CONSERVATIVE}:
            raise ValueError("only broker-backed portfolios can be reconciled")
        self.store = store
        self.health = health
        self.agent = agent
        self.broker = broker
        self.portfolio_id = f"{agent.value}_codex"

    def reconcile(self, *, decision_cycle_id: str | None = None) -> ReconciliationResult:
        at = datetime.now(UTC)
        local_raw = self.store.latest_portfolio_snapshot(self.portfolio_id)
        local = None if local_raw is None else PortfolioSnapshot.model_validate(local_raw)
        try:
            broker = self.broker.account_snapshot(
                self.portfolio_id,
                peak_equity=None if local is None else local.peak_equity,
                expected_positions=None,
                realized_pnl=Decimal("0") if local is None else local.realized_pnl,
            )
        except Exception as exc:
            self.health.set(
                f"{self.agent.value}_alpaca",
                HealthStatus.UNHEALTHY,
                f"broker state unavailable: {type(exc).__name__}",
                agent=self.agent,
            )
            raise

        self._recover_missing_fills()
        differences = [] if local is None else _differences(local, broker)
        if differences:
            self._recover_known_order_updates(at)
        explained = local is None or self._updates_explain(local, differences)
        healthy = broker.reconciled and (not differences or explained)
        realized_pnl = self._realized_pnl(local)
        authoritative = broker.model_copy(
            update={"reconciled": healthy, "realized_pnl": realized_pnl}
        )
        result = ReconciliationResult(
            agent=self.agent,
            timestamp=at,
            healthy=healthy,
            explained=explained,
            differences=differences,
            broker_snapshot=authoritative,
            broker_open_order_ids=broker.open_order_ids,
        )
        self.store.record("reconciliation_events", result, decision_cycle_id=decision_cycle_id)
        if healthy:
            self.store.record(
                "portfolio_snapshots", authoritative, decision_cycle_id=decision_cycle_id
            )
            for position in authoritative.positions:
                self.store.record(
                    "positions",
                    {
                        "portfolio_id": authoritative.portfolio_id,
                        "timestamp": authoritative.timestamp,
                        **position.model_dump(mode="json"),
                    },
                    decision_cycle_id=decision_cycle_id,
                )
            self.store.record(
                "equity_history",
                {
                    "portfolio_id": authoritative.portfolio_id,
                    "timestamp": authoritative.timestamp,
                    "equity": authoritative.equity,
                },
                decision_cycle_id=decision_cycle_id,
            )
            self.health.set(
                f"{self.agent.value}_portfolio_reconciliation",
                HealthStatus.HEALTHY,
                "broker and database state reconciled"
                if local is not None
                else "broker state established at startup",
                agent=self.agent,
            )
            self.health.set(
                f"{self.agent.value}_alpaca",
                HealthStatus.HEALTHY,
                "paper broker state available",
                agent=self.agent,
            )
        else:
            message = "; ".join(differences) or "broker account reports blocked state"
            self.health.set(
                f"{self.agent.value}_portfolio_reconciliation",
                HealthStatus.UNHEALTHY,
                message,
                agent=self.agent,
            )
        return result

    def _updates_explain(
        self,
        local: PortfolioSnapshot,
        differences: list[str],
    ) -> bool:
        if not differences:
            return True
        update_rows = self.store.records("order_updates")
        relevant = [
            row
            for row in update_rows
            if row.get("agent") == self.agent.value
            and datetime.fromisoformat(row["timestamp"]).astimezone(UTC) >= local.timestamp
        ]
        if not relevant:
            return False
        lifecycle_statuses = {
            "submitted",
            "accepted",
            "partially_filled",
            "filled",
            "cancelled",
            "rejected",
        }
        financial_state_changed = any(
            difference.startswith(("cash differs", "position quantities differ"))
            for difference in differences
        )
        if financial_state_changed:
            return any(Decimal(str(row.get("fill_delta_quantity", 0))) > 0 for row in relevant)
        return any(row.get("status") in lifecycle_statuses for row in relevant)

    def _recover_known_order_updates(self, at: datetime) -> None:
        get_order_status = getattr(self.broker, "get_order_status", None)
        if not callable(get_order_status):
            return
        existing = self.store.records_with_metadata("order_updates")
        seen_order_ids: set[str] = set()
        for row in reversed(self.store.records_with_metadata("orders")):
            if row.get("path") != "codex" or row.get("agent") != self.agent.value:
                continue
            order_id = row.get("order_id")
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(str(order_id))
            try:
                state = get_order_status(str(order_id))
            except Exception:
                continue
            status = (
                state.status
                if isinstance(state.status, OrderStatus)
                else OrderStatus(str(getattr(state.status, "value", state.status)))
            )
            cumulative = Decimal(str(state.filled_quantity))
            matching = [item for item in existing if item.get("order_id") == str(order_id)]
            if any(
                item.get("status") == status.value
                and Decimal(str(item.get("cumulative_filled_quantity", 0))) == cumulative
                for item in matching
            ):
                continue
            previous = max(
                (Decimal(str(item.get("cumulative_filled_quantity", 0))) for item in matching),
                default=Decimal("0"),
            )
            delta = max(Decimal("0"), cumulative - previous)
            action = TradeAction(str(row["action"]))
            update = TradeUpdate(
                agent=self.agent,
                order_id=str(order_id),
                client_order_id=str(state.client_order_id),
                symbol=str(state.symbol),
                action=action,
                status=status,
                cumulative_filled_quantity=cumulative,
                fill_delta_quantity=delta,
                fill_price=state.average_fill_price,
                timestamp=at,
            )
            cycle_id = row.get("_decision_cycle_id")
            self.store.record("order_updates", update, decision_cycle_id=cycle_id)
            if delta > 0 and update.fill_price is not None:
                self.store.record(
                    "fills",
                    ExecutedTrade(
                        trade_id=update.event_id,
                        order_id=update.order_id,
                        agent=self.agent,
                        symbol=update.symbol,
                        action=action,
                        quantity=delta,
                        price=update.fill_price,
                        executed_at=at,
                        status=status,
                    ),
                    decision_cycle_id=cycle_id,
                )

    def _recover_missing_fills(self) -> None:
        orders = self.store.records_with_metadata("orders")
        orders_by_id = {
            str(row["order_id"]): row
            for row in orders
            if row.get("path") == "codex"
            and row.get("agent") == self.agent.value
            and row.get("order_id")
        }
        recorded_trade_ids = {
            str(row.get("trade_id")) for row in self.store.records_with_metadata("fills")
        }
        for update in self.store.records_with_metadata("order_updates"):
            if update.get("agent") != self.agent.value:
                continue
            event_id = str(update.get("event_id"))
            delta = Decimal(str(update.get("fill_delta_quantity", 0)))
            price = update.get("fill_price")
            if event_id in recorded_trade_ids or delta <= 0 or price is None:
                continue
            order = orders_by_id.get(str(update.get("order_id")))
            raw_action = update.get("action") or (None if order is None else order.get("action"))
            if raw_action is None:
                continue
            action = TradeAction(str(raw_action))
            self.store.record(
                "fills",
                ExecutedTrade(
                    trade_id=event_id,
                    order_id=str(update["order_id"]),
                    agent=self.agent,
                    symbol=str(update["symbol"]),
                    action=action,
                    quantity=delta,
                    price=Decimal(str(price)),
                    executed_at=datetime.fromisoformat(str(update["timestamp"])).astimezone(UTC),
                    status=OrderStatus(str(update["status"])),
                ),
                decision_cycle_id=update.get("_decision_cycle_id"),
            )
            recorded_trade_ids.add(event_id)

    def _realized_pnl(self, local: PortfolioSnapshot | None) -> Decimal:
        if local is None:
            return Decimal("0")
        result = local.realized_pnl
        average_prices = {item.symbol: item.average_entry_price for item in local.positions}
        for row in self.store.records("order_updates"):
            if (
                row.get("agent") != self.agent.value
                or row.get("action") != "SELL"
                or not row.get("fill_price")
                or datetime.fromisoformat(row["timestamp"]).astimezone(UTC) < local.timestamp
            ):
                continue
            average = average_prices.get(str(row.get("symbol")))
            if average is None:
                continue
            quantity = Decimal(str(row.get("fill_delta_quantity", 0)))
            result += quantity * (Decimal(str(row["fill_price"])) - average)
        return result


def _differences(local: PortfolioSnapshot, broker: PortfolioSnapshot) -> list[str]:
    result: list[str] = []
    if local.cash != broker.cash:
        result.append(f"cash differs (local={local.cash}, broker={broker.cash})")
    local_positions = {item.symbol: item.quantity for item in local.positions}
    broker_positions = {item.symbol: item.quantity for item in broker.positions}
    if local_positions != broker_positions:
        result.append("position quantities differ")
    if set(local.open_order_ids) != set(broker.open_order_ids):
        result.append("open orders differ")
    return result
