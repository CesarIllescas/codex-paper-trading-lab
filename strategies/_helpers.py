from __future__ import annotations

from core.models import Signal, StrategyContext, TradeAction


def signal(
    context: StrategyContext,
    strategy: str,
    score: float,
    rationale: str,
) -> Signal:
    bounded = max(-1.0, min(1.0, score))
    if bounded > 0.1:
        action = TradeAction.BUY
    elif bounded < -0.1:
        action = TradeAction.SELL
    else:
        action = TradeAction.HOLD
    return Signal(
        strategy=strategy,
        symbol=context.market.symbol,
        action=action,
        score=bounded,
        confidence=abs(bounded),
        timestamp=context.market.decision_timestamp,
        rationale=rationale,
    )


def number(context: StrategyContext, name: str) -> float | None:
    value = context.indicator_values.get(name)
    return None if value is None else float(value)
