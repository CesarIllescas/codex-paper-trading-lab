from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.models import (
    AgentType,
    ExecutedTrade,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    TradeAction,
)


class ShadowPortfolio:
    """An isolated local portfolio that never owns or calls a broker."""

    def __init__(
        self,
        portfolio_id: AgentType,
        initial_cash: Decimal,
        *,
        _allow_historical_codex: bool = False,
    ) -> None:
        quant_agents = {AgentType.AGGRESSIVE_QUANT, AgentType.CONSERVATIVE_QUANT}
        if portfolio_id not in quant_agents and not _allow_historical_codex:
            raise ValueError("only quant baselines can be shadow portfolios")
        self.portfolio_id = portfolio_id
        self.initial_cash = initial_cash
        self.cash = initial_cash
        self.realized_pnl = Decimal("0")
        self._positions: dict[str, tuple[Decimal, Decimal]] = {}
        self.trade_history: list[ExecutedTrade] = []
        self.equity_history: list[tuple[datetime, Decimal]] = []
        self.peak_equity = initial_cash

    @classmethod
    def from_snapshot(
        cls, portfolio_id: AgentType, initial_cash: Decimal, snapshot: PortfolioSnapshot
    ) -> ShadowPortfolio:
        if snapshot.portfolio_id != portfolio_id.value:
            raise ValueError("portfolio isolation violation while restoring snapshot")
        portfolio = cls(portfolio_id, initial_cash)
        portfolio.cash = snapshot.cash
        portfolio.realized_pnl = snapshot.realized_pnl
        portfolio.peak_equity = snapshot.peak_equity
        portfolio._positions = {
            item.symbol: (item.quantity, item.average_entry_price) for item in snapshot.positions
        }
        return portfolio

    def execute(self, decision: RiskDecision, price: Decimal, at: datetime) -> ExecutedTrade:
        if not decision.approved or decision.order_request is None:
            raise ValueError("shadow execution requires deterministic risk approval")
        order = decision.order_request
        if order.agent is not self.portfolio_id:
            raise ValueError("portfolio isolation violation")
        return self.execute_fill(
            decision,
            price=price,
            quantity=order.quantity,
            fees=Decimal("0"),
            at=at,
        )

    def execute_fill(
        self,
        decision: RiskDecision,
        *,
        price: Decimal,
        quantity: Decimal,
        fees: Decimal,
        at: datetime,
        status: OrderStatus = OrderStatus.FILLED,
    ) -> ExecutedTrade:
        if not decision.approved or decision.order_request is None:
            raise ValueError("shadow execution requires deterministic risk approval")
        order = decision.order_request
        if order.agent is not self.portfolio_id:
            raise ValueError("portfolio isolation violation")
        if quantity <= 0 or quantity > order.quantity:
            raise ValueError("fill quantity must be positive and no greater than the order")
        if price <= 0 or fees < 0:
            raise ValueError("fill price and fees are invalid")
        current_quantity, average = self._positions.get(order.symbol, (Decimal("0"), price))
        if order.action is TradeAction.BUY:
            cost = quantity * price + fees
            if cost > self.cash:
                raise ValueError("shadow portfolio has insufficient cash")
            new_quantity = current_quantity + quantity
            new_average = ((current_quantity * average) + cost) / new_quantity
            self._positions[order.symbol] = (new_quantity, new_average)
            self.cash -= cost
        elif order.action is TradeAction.SELL:
            if quantity > current_quantity:
                raise ValueError("short selling is prohibited")
            self.cash += quantity * price - fees
            self.realized_pnl += quantity * (price - average) - fees
            remaining = current_quantity - quantity
            if remaining:
                self._positions[order.symbol] = (remaining, average)
            else:
                self._positions.pop(order.symbol, None)
        else:
            raise ValueError("HOLD cannot execute")
        trade = ExecutedTrade(
            trade_id=str(uuid4()),
            order_id=f"shadow-{uuid4()}",
            agent=self.portfolio_id,
            symbol=order.symbol,
            action=order.action,
            quantity=quantity,
            price=price,
            executed_at=at.astimezone(UTC),
            status=status,
            fees=fees,
        )
        self.trade_history.append(trade)
        return trade

    def snapshot(
        self,
        prices: dict[str, Decimal],
        at: datetime | None = None,
    ) -> PortfolioSnapshot:
        positions: list[Position] = []
        unrealized = Decimal("0")
        exposure = Decimal("0")
        for symbol, (quantity, average) in self._positions.items():
            if symbol not in prices:
                raise ValueError(f"missing current price for {symbol}; state is uncertain")
            current = prices[symbol]
            market_value = quantity * current
            pnl = quantity * (current - average)
            exposure += market_value
            unrealized += pnl
            positions.append(
                Position(
                    symbol=symbol,
                    quantity=quantity,
                    average_entry_price=average,
                    current_price=current,
                    market_value=market_value,
                    unrealized_pnl=pnl,
                )
            )
        equity = self.cash + exposure
        self.peak_equity = max(self.peak_equity, equity)
        drawdown = (self.peak_equity - equity) / self.peak_equity
        timestamp = (at or datetime.now(UTC)).astimezone(UTC)
        self.equity_history.append((timestamp, equity))
        return PortfolioSnapshot(
            portfolio_id=self.portfolio_id.value,
            timestamp=timestamp,
            cash=self.cash,
            equity=equity,
            buying_power=self.cash,
            positions=tuple(positions),
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            peak_equity=self.peak_equity,
            drawdown_pct=drawdown,
            exposure=exposure,
        )


class HistoricalPortfolio(ShadowPortfolio):
    """Brokerless portfolio used only by optional historical Codex replay."""

    def __init__(self, portfolio_id: AgentType, initial_cash: Decimal) -> None:
        if portfolio_id not in {AgentType.AGGRESSIVE, AgentType.CONSERVATIVE}:
            raise ValueError("historical Codex portfolios require a production agent type")
        super().__init__(
            portfolio_id,
            initial_cash,
            _allow_historical_codex=True,
        )
