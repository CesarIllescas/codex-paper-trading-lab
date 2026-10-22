from decimal import Decimal

import pytest

from core.models import AgentType, OrderRequest, RiskDecision, TradeAction
from portfolio.shadow import ShadowPortfolio


def approved(agent: AgentType, action: TradeAction, quantity: str = "2") -> RiskDecision:
    return RiskDecision(
        approved=True,
        reasons=["test"],
        order_request=OrderRequest(
            agent=agent,
            symbol="BTC/USD",
            action=action,
            quantity=Decimal(quantity),
            estimated_notional=Decimal(quantity) * Decimal("100"),
            client_order_id="test-order",
        ),
    )


def test_shadow_execution_and_realized_pnl(now: object) -> None:
    portfolio = ShadowPortfolio(AgentType.AGGRESSIVE_QUANT, Decimal("1000"))
    portfolio.execute(approved(AgentType.AGGRESSIVE_QUANT, TradeAction.BUY), Decimal("100"), now)  # type: ignore[arg-type]
    portfolio.execute(approved(AgentType.AGGRESSIVE_QUANT, TradeAction.SELL), Decimal("110"), now)  # type: ignore[arg-type]
    snapshot = portfolio.snapshot({}, now)  # type: ignore[arg-type]
    assert snapshot.cash == Decimal("1020")
    assert snapshot.realized_pnl == Decimal("20")
    assert snapshot.positions == ()


def test_portfolio_isolation(now: object) -> None:
    aggressive = ShadowPortfolio(AgentType.AGGRESSIVE_QUANT, Decimal("1000"))
    conservative = ShadowPortfolio(AgentType.CONSERVATIVE_QUANT, Decimal("1000"))
    with pytest.raises(ValueError, match="isolation"):
        aggressive.execute(
            approved(AgentType.CONSERVATIVE_QUANT, TradeAction.BUY),
            Decimal("100"),
            now,  # type: ignore[arg-type]
        )
    assert conservative.snapshot({}, now).cash == Decimal("1000")  # type: ignore[arg-type]
