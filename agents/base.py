from __future__ import annotations

from typing import Protocol

from core.models import (
    AgentType,
    MarketSnapshot,
    PortfolioSnapshot,
    Signal,
    TradeAction,
    TradeIntent,
    hold_intent,
)
from intelligence.schemas import CodexOutcome


class IntelligenceClient(Protocol):
    async def decide(
        self,
        market: MarketSnapshot,
        signals: list[Signal],
        portfolio: PortfolioSnapshot,
        recent_decisions: list[dict[str, object]] | None = None,
    ) -> CodexOutcome: ...


class TradingAgent:
    def __init__(
        self,
        agent_type: AgentType,
        intelligence: IntelligenceClient,
        minimum_confidence: float,
    ) -> None:
        self.agent_type = agent_type
        self.intelligence = intelligence
        self.minimum_confidence = minimum_confidence

    async def create_intent(
        self,
        market: MarketSnapshot,
        signals: list[Signal],
        portfolio: PortfolioSnapshot,
        recent_decisions: list[dict[str, object]] | None = None,
    ) -> TradeIntent:
        outcome = await self.intelligence.decide(
            market, signals, portfolio, recent_decisions=recent_decisions
        )
        if not outcome.valid or outcome.decision is None:
            return hold_intent(
                self.agent_type,
                market.symbol,
                market.decision_timestamp,
                "Codex response unavailable or invalid",
                codex_valid=False,
            )
        decision = outcome.decision
        action = decision.action
        risk_flags = list(decision.risk_flags)
        thesis = decision.thesis
        if action is not TradeAction.HOLD and decision.confidence < self.minimum_confidence:
            action = TradeAction.HOLD
            risk_flags.append("below configured minimum Codex confidence")
            thesis = f"HOLD: confidence gate rejected decision. {thesis}"
        return TradeIntent(
            agent=self.agent_type,
            action=action,
            symbol=decision.symbol,
            confidence=decision.confidence,
            strategy_bias=decision.strategy_bias,
            thesis=thesis,
            suggested_stop_loss_pct=decision.suggested_stop_loss_pct,
            suggested_take_profit_pct=decision.suggested_take_profit_pct,
            risk_flags=risk_flags,
            codex_valid=True,
            decision_timestamp=market.decision_timestamp,
        )
