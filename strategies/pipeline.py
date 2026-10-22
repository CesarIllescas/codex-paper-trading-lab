from __future__ import annotations

from config.settings import Settings
from core.models import AgentType, MarketSnapshot, Signal, StrategyContext
from strategies.mean_reversion import mean_reversion_signal
from strategies.momentum import momentum_signal
from strategies.trend_following import trend_following_signal


def generate_signals(
    settings: Settings,
    agent: AgentType,
    market: MarketSnapshot,
) -> list[Signal]:
    """Run the configured deterministic strategies without external side effects."""
    base_agent = (
        AgentType.AGGRESSIVE
        if agent in {AgentType.AGGRESSIVE, AgentType.AGGRESSIVE_QUANT}
        else AgentType.CONSERVATIVE
    )
    context = StrategyContext(
        agent=agent,
        market=market,
        indicator_values=market.indicators,
    )
    functions = {
        "momentum": momentum_signal,
        "mean_reversion": mean_reversion_signal,
        "trend_following": trend_following_signal,
    }
    toggles = settings.agents[base_agent.value].strategies
    return [
        functions[name](context)
        for name, toggle in toggles.items()
        if toggle.enabled and name in functions
    ]


def strategy_weights(settings: Settings, agent: AgentType) -> dict[str, float]:
    base_agent = (
        AgentType.AGGRESSIVE
        if agent in {AgentType.AGGRESSIVE, AgentType.AGGRESSIVE_QUANT}
        else AgentType.CONSERVATIVE
    )
    return {
        name: toggle.weight
        for name, toggle in settings.agents[base_agent.value].strategies.items()
        if toggle.enabled
    }
