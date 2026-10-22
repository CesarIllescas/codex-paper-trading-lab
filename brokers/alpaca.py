from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.models import (
    AgentType,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    TradeAction,
)


@dataclass(frozen=True, slots=True)
class BrokerOrderState:
    order_id: str
    client_order_id: str
    symbol: str
    status: OrderStatus
    requested_quantity: Decimal
    filled_quantity: Decimal
    average_fill_price: Decimal | None

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED


def _status(value: Any) -> OrderStatus:
    text = str(getattr(value, "value", value)).lower()
    mapping = {
        "new": OrderStatus.ACCEPTED,
        "pending_new": OrderStatus.SUBMITTED,
        "accepted": OrderStatus.ACCEPTED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
    }
    return mapping.get(text, OrderStatus.SUBMITTED)


def normalize_order(order: Any) -> BrokerOrderState:
    average = getattr(order, "filled_avg_price", None)
    return BrokerOrderState(
        order_id=str(order.id),
        client_order_id=str(order.client_order_id),
        symbol=_normalize_symbol(str(order.symbol)),
        status=_status(order.status),
        requested_quantity=Decimal(str(order.qty)),
        filled_quantity=Decimal(str(getattr(order, "filled_qty", 0) or 0)),
        average_fill_price=None if average is None else Decimal(str(average)),
    )


class AlpacaBrokerAdapter:
    def __init__(self, agent: AgentType, trading_client: Any, *, paper: bool) -> None:
        if agent not in {AgentType.AGGRESSIVE, AgentType.CONSERVATIVE}:
            raise ValueError("shadow portfolios must never have broker adapters")
        if paper is not True:
            raise RuntimeError("live Alpaca clients are prohibited")
        self.agent = agent
        self._client = trading_client

    def submit_approved(self, decision: RiskDecision) -> BrokerOrderState:
        if not decision.approved or decision.order_request is None:
            raise ValueError("broker accepts only an approved RiskDecision")
        request = decision.order_request
        if request.agent is not self.agent:
            raise ValueError("order belongs to another isolated broker account")
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        order_data = MarketOrderRequest(
            symbol=request.symbol,
            qty=float(request.quantity),
            side=OrderSide.BUY if request.action is TradeAction.BUY else OrderSide.SELL,
            time_in_force=(TimeInForce.GTC if request.time_in_force == "gtc" else TimeInForce.DAY),
            client_order_id=request.client_order_id,
        )
        return normalize_order(self._client.submit_order(order_data=order_data))

    def get_order_status(self, order_id: str) -> BrokerOrderState:
        return normalize_order(self._client.get_order_by_id(order_id))

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)

    def open_orders(self) -> tuple[BrokerOrderState, ...]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self._client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
        return tuple(normalize_order(order) for order in orders)

    def calendar_sessions(
        self, start: datetime, end: datetime
    ) -> tuple[tuple[datetime, datetime], ...]:
        from alpaca.trading.requests import GetCalendarRequest

        entries = self._client.get_calendar(
            filters=GetCalendarRequest(start=start.date(), end=end.date())
        )
        return tuple((entry.open.astimezone(UTC), entry.close.astimezone(UTC)) for entry in entries)

    def current_positions(self) -> tuple[Position, ...]:
        raw_positions = self._client.get_all_positions()
        if any(Decimal(str(item.qty)) < 0 for item in raw_positions):
            raise RuntimeError("short position detected; refusing to trade")
        return tuple(
            Position(
                symbol=_normalize_symbol(str(item.symbol)),
                quantity=Decimal(str(item.qty)),
                average_entry_price=Decimal(str(item.avg_entry_price)),
                current_price=Decimal(str(item.current_price)),
                market_value=Decimal(str(item.market_value)),
                unrealized_pnl=Decimal(str(item.unrealized_pl)),
            )
            for item in raw_positions
        )

    def account_snapshot(
        self,
        portfolio_id: str,
        *,
        peak_equity: Decimal | None = None,
        expected_positions: tuple[Position, ...] | None = None,
        realized_pnl: Decimal = Decimal("0"),
    ) -> PortfolioSnapshot:
        account = self._client.get_account()
        positions = self.current_positions()
        open_order_ids = tuple(item.order_id for item in self.open_orders())
        reconciled = True
        if expected_positions is not None:
            actual = {item.symbol: item.quantity for item in positions}
            expected = {item.symbol: item.quantity for item in expected_positions}
            reconciled = actual == expected
        equity = Decimal(str(account.equity))
        last_equity = Decimal(str(getattr(account, "last_equity", equity)))
        peak = max(peak_equity or equity, equity)
        exposure = sum((item.market_value for item in positions), Decimal("0"))
        drawdown = (peak - equity) / peak if peak else Decimal("0")
        return PortfolioSnapshot(
            portfolio_id=portfolio_id,
            timestamp=datetime.now(UTC),
            cash=Decimal(str(account.cash)),
            equity=equity,
            buying_power=Decimal(str(account.buying_power)),
            positions=positions,
            realized_pnl=realized_pnl,
            unrealized_pnl=sum((item.unrealized_pnl for item in positions), Decimal("0")),
            daily_pnl=equity - last_equity,
            peak_equity=peak,
            drawdown_pct=drawdown,
            exposure=exposure,
            reconciled=reconciled
            and not bool(getattr(account, "trading_blocked", False))
            and not bool(getattr(account, "account_blocked", False)),
            open_order_ids=open_order_ids,
        )


def build_alpaca_broker(agent: AgentType) -> AlpacaBrokerAdapter:
    if os.environ.get("TRADING_ENV") != "paper":
        raise RuntimeError("TRADING_ENV must be paper before creating an Alpaca client")
    from alpaca.trading.client import TradingClient

    prefix = "ALPACA_AGGRESSIVE" if agent is AgentType.AGGRESSIVE else "ALPACA_CONSERVATIVE"
    key = os.environ.get(f"{prefix}_API_KEY")
    secret = os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"missing paper credentials for {agent.value}")
    client = TradingClient(key, secret, paper=True)
    return AlpacaBrokerAdapter(agent, client, paper=True)


def _normalize_symbol(symbol: str) -> str:
    return {
        "BTCUSD": "BTC/USD",
        "ETHUSD": "ETH/USD",
        "SOLUSD": "SOL/USD",
    }.get(symbol, symbol)
