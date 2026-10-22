from dataclasses import dataclass
from decimal import Decimal

import pytest

from brokers.alpaca import AlpacaBrokerAdapter, normalize_order
from core.models import AgentType, OrderRequest, OrderStatus, RiskDecision, TradeAction


@dataclass
class FakeOrder:
    status: str
    filled_qty: str = "0"
    filled_avg_price: str | None = None
    id: str = "broker-id"
    client_order_id: str = "client-id"
    symbol: str = "SPY"
    qty: str = "10"


class FakeTradingClient:
    def __init__(self, order: FakeOrder) -> None:
        self.order = order

    def submit_order(self, order_data: object) -> FakeOrder:
        return self.order


@dataclass
class FakePosition:
    symbol: str = "SOLUSD"
    qty: str = "2"
    avg_entry_price: str = "100"
    current_price: str = "101"
    market_value: str = "202"
    unrealized_pl: str = "2"


class FakePositionClient:
    def get_all_positions(self) -> list[FakePosition]:
        return [FakePosition()]


def risk_decision() -> RiskDecision:
    return RiskDecision(
        approved=True,
        reasons=["approved"],
        order_request=OrderRequest(
            agent=AgentType.CONSERVATIVE,
            symbol="SPY",
            action=TradeAction.BUY,
            quantity=Decimal("10"),
            estimated_notional=Decimal("1000"),
            client_order_id="client-id",
        ),
    )


def test_broker_rejection_is_not_a_fill() -> None:
    adapter = AlpacaBrokerAdapter(
        AgentType.CONSERVATIVE, FakeTradingClient(FakeOrder("rejected")), paper=True
    )
    result = adapter.submit_approved(risk_decision())
    assert result.status is OrderStatus.REJECTED
    assert not result.is_filled


def test_partial_and_accepted_orders_are_not_filled() -> None:
    partial = normalize_order(FakeOrder("partially_filled", "4", "101"))
    accepted = normalize_order(FakeOrder("accepted"))
    assert partial.status is OrderStatus.PARTIALLY_FILLED
    assert partial.filled_quantity == Decimal("4")
    assert not partial.is_filled
    assert accepted.status is OrderStatus.ACCEPTED
    assert not accepted.is_filled


def test_live_broker_adapter_is_impossible() -> None:
    with pytest.raises(RuntimeError, match="live"):
        AlpacaBrokerAdapter(AgentType.AGGRESSIVE, object(), paper=False)


def test_crypto_broker_position_symbol_is_normalized() -> None:
    adapter = AlpacaBrokerAdapter(AgentType.AGGRESSIVE, FakePositionClient(), paper=True)
    assert adapter.current_positions()[0].symbol == "SOL/USD"
