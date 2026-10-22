from decimal import Decimal

import pytest

from core.models import MarketSnapshot, PortfolioSnapshot, TradeAction
from risk.aggressive import AggressiveRiskEngine
from risk.kill_switch import KillSwitchState
from tests.conftest import make_position


def test_risk_per_trade_and_position_size_are_capped(
    limits: object, market: object, portfolio: object, buy_intent: object
) -> None:
    decision = AggressiveRiskEngine(limits).evaluate(buy_intent, portfolio, market)  # type: ignore[arg-type]
    assert decision.approved
    assert decision.order_request is not None
    assert decision.order_request.estimated_notional <= Decimal("1500")
    assert decision.risk_amount <= Decimal("200")


def test_maximum_exposure_rejects_buy(
    limits: object,
    market: object,
    portfolio: PortfolioSnapshot,
    buy_intent: object,
) -> None:
    full = portfolio.model_copy(update={"exposure": Decimal("6000")})
    decision = AggressiveRiskEngine(limits).evaluate(buy_intent, full, market)  # type: ignore[arg-type]
    assert not decision.approved
    assert "exposure" in decision.reasons[0]


def test_maximum_positions_and_buying_power(
    limits: object,
    market: object,
    portfolio: PortfolioSnapshot,
    buy_intent: object,
) -> None:
    positions = tuple(make_position(f"S{i}") for i in range(4))
    full = portfolio.model_copy(update={"positions": positions})
    assert not AggressiveRiskEngine(limits).evaluate(buy_intent, full, market).approved  # type: ignore[arg-type]
    no_cash = portfolio.model_copy(update={"buying_power": Decimal("0")})
    assert not AggressiveRiskEngine(limits).evaluate(buy_intent, no_cash, market).approved  # type: ignore[arg-type]
    margin_only = portfolio.model_copy(update={"cash": Decimal("0")})
    assert not AggressiveRiskEngine(limits).evaluate(buy_intent, margin_only, market).approved  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("portfolio_updates", "market_updates", "state", "reason"),
    [
        ({"daily_pnl": Decimal("-600")}, {}, KillSwitchState(), "daily loss"),
        ({"drawdown_pct": Decimal("0.20")}, {}, KillSwitchState(), "drawdown"),
        ({"reconciled": False}, {}, KillSwitchState(), "reconciled"),
        ({}, {"stale": True}, KillSwitchState(), "stale"),
        ({}, {}, KillSwitchState(duplicate_order=True), "duplicate"),
        ({}, {}, KillSwitchState(api_error_count=3), "API errors"),
    ],
)
def test_kill_switch_conditions(
    limits: object,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    buy_intent: object,
    portfolio_updates: dict[str, object],
    market_updates: dict[str, object],
    state: KillSwitchState,
    reason: str,
) -> None:
    result = AggressiveRiskEngine(limits).evaluate(  # type: ignore[arg-type]
        buy_intent,
        portfolio.model_copy(update=portfolio_updates),
        market.model_copy(update=market_updates),
        state,
    )
    assert not result.approved
    assert any(reason in item for item in result.reasons)


def test_invalid_price_codex_failure_and_short_sale_are_rejected(
    limits: object,
    market: MarketSnapshot,
    portfolio: PortfolioSnapshot,
    buy_intent: object,
) -> None:
    invalid_market = market.model_copy(update={"price": Decimal("0")})
    result = AggressiveRiskEngine(limits).evaluate(buy_intent, portfolio, invalid_market)  # type: ignore[arg-type]
    assert not result.approved
    invalid_codex = buy_intent.model_copy(update={"codex_valid": False})  # type: ignore[attr-defined]
    assert not AggressiveRiskEngine(limits).evaluate(invalid_codex, portfolio, market).approved  # type: ignore[arg-type]
    sell = buy_intent.model_copy(update={"action": TradeAction.SELL})  # type: ignore[attr-defined]
    assert not AggressiveRiskEngine(limits).evaluate(sell, portfolio, market).approved  # type: ignore[arg-type]
