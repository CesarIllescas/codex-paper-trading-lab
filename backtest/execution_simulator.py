from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.models import Candle, ExecutedTrade, OrderStatus, RiskDecision, TradeAction
from portfolio.shadow import ShadowPortfolio


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    slippage_bps: Decimal = Decimal("2")
    transaction_cost_bps: Decimal = Decimal("1")
    fill_fraction: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.slippage_bps < 0 or self.transaction_cost_bps < 0:
            raise ValueError("execution costs cannot be negative")
        if not Decimal("0") < self.fill_fraction <= Decimal("1"):
            raise ValueError("fill fraction must be in (0, 1]")


class HistoricalExecutionSimulator:
    """Deterministically fill a market order at the next bar's open."""

    def __init__(self, assumptions: ExecutionAssumptions) -> None:
        self.assumptions = assumptions

    def execute(
        self,
        decision: RiskDecision,
        next_bar: Candle,
        portfolio: ShadowPortfolio,
        at: datetime,
    ) -> ExecutedTrade:
        if not decision.approved or decision.order_request is None:
            raise ValueError("simulation requires an approved order")
        order = decision.order_request
        slippage = self.assumptions.slippage_bps / Decimal("10000")
        multiplier = Decimal("1") + slippage
        if order.action is TradeAction.SELL:
            multiplier = Decimal("1") - slippage
        fill_price = next_bar.open * multiplier
        quantity = order.quantity * self.assumptions.fill_fraction
        fee = quantity * fill_price * self.assumptions.transaction_cost_bps / Decimal("10000")
        status = (
            OrderStatus.FILLED
            if self.assumptions.fill_fraction == Decimal("1")
            else OrderStatus.PARTIALLY_FILLED
        )
        return portfolio.execute_fill(
            decision,
            price=fill_price,
            quantity=quantity,
            fees=fee,
            at=at,
            status=status,
        )
