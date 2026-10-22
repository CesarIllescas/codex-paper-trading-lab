from __future__ import annotations

from core.models import AgentType, Signal, TradeAction, TradeIntent


def deterministic_baseline(
    agent: AgentType,
    signals: list[Signal],
    weights: dict[str, float],
) -> TradeIntent:
    if agent not in {AgentType.AGGRESSIVE_QUANT, AgentType.CONSERVATIVE_QUANT}:
        raise ValueError("deterministic baseline requires a quant portfolio agent")
    if not signals:
        raise ValueError("baseline requires strategy signals")
    weighted = sum(item.score * weights.get(item.strategy, 0.0) for item in signals)
    total_weight = sum(weights.get(item.strategy, 0.0) for item in signals)
    normalized = weighted / total_weight if total_weight else 0.0
    if normalized > 0.15:
        action = TradeAction.BUY
    elif normalized < -0.15:
        action = TradeAction.SELL
    else:
        action = TradeAction.HOLD
    return TradeIntent(
        agent=agent,
        action=action,
        symbol=signals[0].symbol,
        confidence=min(1.0, abs(normalized)),
        strategy_bias="deterministic_weighted_vote",
        thesis=f"weighted deterministic score={normalized:.4f}",
        codex_valid=True,
        decision_timestamp=signals[0].timestamp,
    )
