from config.settings import RiskLimits
from core.models import AgentType, MarketSnapshot, PortfolioSnapshot, RiskDecision, TradeIntent
from risk.base import DeterministicRiskEngine, validate_agent
from risk.kill_switch import KillSwitchState


class ConservativeRiskEngine(DeterministicRiskEngine):
    def __init__(self, limits: RiskLimits) -> None:
        super().__init__(limits)

    def evaluate(
        self,
        intent: TradeIntent,
        portfolio: PortfolioSnapshot,
        market: MarketSnapshot,
        state: KillSwitchState | None = None,
    ) -> RiskDecision:
        validate_agent(intent, {AgentType.CONSERVATIVE, AgentType.CONSERVATIVE_QUANT})
        return super().evaluate(intent, portfolio, market, state)
