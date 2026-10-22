from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class AgentType(StrEnum):
    AGGRESSIVE = "aggressive"
    CONSERVATIVE = "conservative"
    AGGRESSIVE_QUANT = "aggressive_quant"
    CONSERVATIVE_QUANT = "conservative_quant"


class TradeAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class OrderStatus(StrEnum):
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Candle(DomainModel):
    timestamp: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    is_closed: bool = True

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value

    @model_validator(mode="after")
    def prices_are_possible(self) -> Candle:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high price is below another OHLC price")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low price is above another OHLC price")
        return self


class Signal(DomainModel):
    strategy: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    action: TradeAction
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    timestamp: datetime
    rationale: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value


class MarketSnapshot(DomainModel):
    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    symbol: str = Field(min_length=1)
    decision_timestamp: datetime
    price: Decimal = Field(gt=0)
    latest_bar_at: datetime
    candles: tuple[Candle, ...] = ()
    indicators: dict[str, float | None] = Field(default_factory=dict)
    stale: bool = False

    @field_validator("decision_timestamp", "latest_bar_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_timeline(self) -> MarketSnapshot:
        if self.latest_bar_at > self.decision_timestamp:
            raise ValueError("market data cannot be from the future")
        timestamps = [bar.timestamp for bar in self.candles]
        if timestamps != sorted(timestamps):
            raise ValueError("candles must be chronological")
        if len(timestamps) != len(set(timestamps)):
            raise ValueError("duplicate candles are not allowed")
        if any(bar.timestamp > self.decision_timestamp for bar in self.candles):
            raise ValueError("candle after decision timestamp would introduce look-ahead bias")
        if any(not bar.is_closed for bar in self.candles):
            raise ValueError("incomplete candles are not allowed")
        return self


class StrategyContext(DomainModel):
    agent: AgentType
    market: MarketSnapshot
    indicator_values: dict[str, float | None]


class CodexDecision(DomainModel):
    action: TradeAction
    symbol: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    strategy_bias: str = Field(min_length=1)
    thesis: str = Field(min_length=1)
    bull_case: list[str]
    bear_case: list[str]
    invalidation_conditions: list[str]
    suggested_stop_loss_pct: float | None = Field(gt=0.0, le=1.0)
    suggested_take_profit_pct: float | None = Field(gt=0.0, le=5.0)
    time_horizon: str = Field(min_length=1)
    risk_flags: list[str]


class TradeIntent(DomainModel):
    agent: AgentType
    action: TradeAction
    symbol: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    strategy_bias: str
    thesis: str
    suggested_stop_loss_pct: float | None = Field(default=None, gt=0.0, le=1.0)
    suggested_take_profit_pct: float | None = Field(default=None, gt=0.0, le=5.0)
    risk_flags: list[str] = Field(default_factory=list)
    codex_valid: bool = True
    decision_timestamp: datetime

    @field_validator("decision_timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decision timestamp must be timezone-aware")
        return value


class OrderRequest(DomainModel):
    agent: AgentType
    symbol: str
    action: TradeAction
    quantity: Decimal = Field(gt=0)
    estimated_notional: Decimal = Field(gt=0)
    client_order_id: str = Field(min_length=1)
    order_type: str = "market"
    time_in_force: str = "day"

    @model_validator(mode="after")
    def action_must_trade(self) -> OrderRequest:
        if self.action is TradeAction.HOLD:
            raise ValueError("a HOLD cannot become an order request")
        return self


class RiskDecision(DomainModel):
    approved: bool
    reasons: list[str]
    order_request: OrderRequest | None = None
    risk_amount: Decimal = Decimal("0")

    @model_validator(mode="after")
    def approval_matches_order(self) -> RiskDecision:
        if self.approved != (self.order_request is not None):
            raise ValueError("approved risk decisions require exactly one order request")
        return self


class ExecutedTrade(DomainModel):
    trade_id: str
    order_id: str
    agent: AgentType
    symbol: str
    action: TradeAction
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    executed_at: datetime
    status: OrderStatus
    fees: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("executed_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("execution timestamp must be timezone-aware")
        return value


class Position(DomainModel):
    symbol: str
    quantity: Decimal = Field(ge=0)
    average_entry_price: Decimal = Field(gt=0)
    current_price: Decimal = Field(gt=0)
    market_value: Decimal = Field(ge=0)
    unrealized_pnl: Decimal = Decimal("0")


class PortfolioSnapshot(DomainModel):
    portfolio_id: str
    timestamp: datetime
    cash: Decimal = Field(ge=0)
    equity: Decimal = Field(gt=0)
    buying_power: Decimal = Field(ge=0)
    positions: tuple[Position, ...] = ()
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    daily_pnl: Decimal = Decimal("0")
    peak_equity: Decimal = Field(gt=0)
    drawdown_pct: Decimal = Field(ge=0, le=1)
    exposure: Decimal = Field(default=Decimal("0"), ge=0)
    reconciled: bool = True
    open_order_ids: tuple[str, ...] = ()

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("portfolio timestamp must be timezone-aware")
        return value


class TradeUpdate(DomainModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    agent: AgentType
    order_id: str
    client_order_id: str
    symbol: str
    action: TradeAction | None = None
    status: OrderStatus
    cumulative_filled_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    fill_delta_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    fill_price: Decimal | None = Field(default=None, gt=0)
    timestamp: datetime

    @field_validator("timestamp")
    @classmethod
    def update_timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("trade update timestamp must be timezone-aware")
        return value


class ReconciliationResult(DomainModel):
    agent: AgentType
    timestamp: datetime
    healthy: bool
    explained: bool = False
    differences: list[str] = Field(default_factory=list)
    broker_snapshot: PortfolioSnapshot
    broker_open_order_ids: tuple[str, ...] = ()

    @field_validator("timestamp")
    @classmethod
    def reconciliation_timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reconciliation timestamp must be timezone-aware")
        return value


def hold_intent(
    agent: AgentType,
    symbol: str,
    timestamp: datetime,
    reason: str,
    *,
    codex_valid: bool = False,
) -> TradeIntent:
    return TradeIntent(
        agent=agent,
        action=TradeAction.HOLD,
        symbol=symbol,
        confidence=0.0,
        strategy_bias="no_decision",
        thesis=reason,
        risk_flags=[reason],
        codex_valid=codex_valid,
        decision_timestamp=timestamp,
    )
