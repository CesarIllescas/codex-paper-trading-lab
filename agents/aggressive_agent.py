from agents.base import IntelligenceClient, TradingAgent
from core.models import AgentType


class AggressiveAgent(TradingAgent):
    def __init__(self, intelligence: IntelligenceClient, minimum_confidence: float) -> None:
        super().__init__(AgentType.AGGRESSIVE, intelligence, minimum_confidence)
