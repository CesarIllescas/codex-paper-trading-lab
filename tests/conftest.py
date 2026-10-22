from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from config.settings import RiskLimits
from core.models import (
    AgentType,
    Candle,
    MarketSnapshot,
    PortfolioSnapshot,
    Position,
    TradeAction,
    TradeIntent,
)


@pytest.fixture
def limits() -> RiskLimits:
    return RiskLimits(
        risk_per_trade=0.02,
        max_position_pct=0.15,
        max_total_exposure_pct=0.60,
        max_daily_loss_pct=0.05,
        max_drawdown_pct=0.20,
        max_open_positions=4,
    )


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 2, 12, tzinfo=UTC)


@pytest.fixture
def market(now: datetime) -> MarketSnapshot:
    candle = Candle(
        timestamp=now - timedelta(minutes=30),
        open=Decimal("99"),
        high=Decimal("101"),
        low=Decimal("98"),
        close=Decimal("100"),
        volume=Decimal("1000"),
    )
    return MarketSnapshot(
        symbol="BTC/USD",
        decision_timestamp=now,
        price=Decimal("100"),
        latest_bar_at=candle.timestamp,
        candles=(candle,),
    )


@pytest.fixture
def portfolio(now: datetime) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        portfolio_id="aggressive_codex",
        timestamp=now,
        cash=Decimal("10000"),
        equity=Decimal("10000"),
        buying_power=Decimal("10000"),
        peak_equity=Decimal("10000"),
        drawdown_pct=Decimal("0"),
    )


@pytest.fixture
def buy_intent(now: datetime) -> TradeIntent:
    return TradeIntent(
        agent=AgentType.AGGRESSIVE,
        action=TradeAction.BUY,
        symbol="BTC/USD",
        confidence=0.8,
        strategy_bias="momentum",
        thesis="test",
        suggested_stop_loss_pct=0.02,
        decision_timestamp=now,
    )


def make_position(symbol: str = "BTC/USD", value: str = "1000") -> Position:
    market_value = Decimal(value)
    return Position(
        symbol=symbol,
        quantity=market_value / Decimal("100"),
        average_entry_price=Decimal("100"),
        current_price=Decimal("100"),
        market_value=market_value,
    )
