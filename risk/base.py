from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from uuid import uuid4

from config.settings import RiskLimits
from core.models import (
    AgentType,
    MarketSnapshot,
    OrderRequest,
    PortfolioSnapshot,
    RiskDecision,
    TradeAction,
    TradeIntent,
)
from risk.kill_switch import KillSwitchState, kill_switch_reasons


class DeterministicRiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        state: KillSwitchState | None = None,
    ) -> RiskDecision:
        state = state or KillSwitchState()
        reasons = kill_switch_reasons(intent, portfolio, market, self.limits, state)
        if intent.symbol != market.symbol:
            reasons.append("intent and market symbols differ")
        if intent.action is TradeAction.HOLD:
            reasons.append("HOLD does not create an order")
        if reasons:
            return RiskDecision(approved=False, reasons=list(dict.fromkeys(reasons)))

        if intent.action is TradeAction.SELL:
            position = next(
                (item for item in portfolio.positions if item.symbol == intent.symbol), None
            )
            if position is None or position.quantity <= 0:
                return RiskDecision(approved=False, reasons=["short selling is prohibited"])
            quantity = position.quantity
            notional = quantity * market.price
            risk_amount = Decimal("0")
        else:
            existing = next(
                (item for item in portfolio.positions if item.symbol == intent.symbol), None
            )
            if existing is None and len(portfolio.positions) >= self.limits.max_open_positions:
                return RiskDecision(approved=False, reasons=["maximum positions reached"])
            equity = portfolio.equity
            risk_budget = equity * Decimal(str(self.limits.risk_per_trade))
            stop_fraction = (
                Decimal(str(intent.suggested_stop_loss_pct))
                if intent.suggested_stop_loss_pct is not None
                else Decimal("1")
            )
            risk_based_notional = risk_budget / stop_fraction
            position_room = equity * Decimal(str(self.limits.max_position_pct))
            if existing is not None:
                position_room -= existing.market_value
            exposure_room = (
                equity * Decimal(str(self.limits.max_total_exposure_pct)) - portfolio.exposure
            )
            notional = min(
                risk_based_notional,
                position_room,
                exposure_room,
                portfolio.buying_power,
                portfolio.cash,
            )
            if notional <= 0:
                return RiskDecision(
                    approved=False,
                    reasons=["insufficient buying power or exposure capacity"],
                )
            quantity = (notional / market.price).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            notional = quantity * market.price
            risk_amount = notional * stop_fraction
            if quantity <= 0 or risk_amount > risk_budget:
                return RiskDecision(approved=False, reasons=["risk-per-trade limit exceeded"])

        request = OrderRequest(
            agent=intent.agent,
            symbol=intent.symbol,
            action=intent.action,
            quantity=quantity,
            estimated_notional=notional,
            client_order_id=f"{intent.agent.value}-{uuid4().hex[:20]}",
            time_in_force="gtc" if "/" in intent.symbol else "day",
        )
        return RiskDecision(
            approved=True,
            reasons=["approved by deterministic risk engine"],
            order_request=request,
            risk_amount=risk_amount,
        )


def validate_agent(intent: TradeIntent, allowed: set[AgentType]) -> None:
    if intent.agent not in allowed:
        raise ValueError("intent belongs to a different isolated risk engine")
