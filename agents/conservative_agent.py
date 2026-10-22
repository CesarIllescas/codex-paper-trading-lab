from agents.base import IntelligenceClient, TradingAgent
from core.models import AgentType


class ConservativeAgent(TradingAgent):
    def __init__(self, intelligence: IntelligenceClient, minimum_confidence: float) -> None:
        super().__init__(AgentType.CONSERVATIVE, intelligence, minimum_confidence)
