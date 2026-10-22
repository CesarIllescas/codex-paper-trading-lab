from core.models import Signal, StrategyContext
from strategies._helpers import number, signal


def momentum_signal(context: StrategyContext) -> Signal:
    return_5 = number(context, "return_5")
    rsi = number(context, "rsi_14")
    volume_change = number(context, "volume_change")
    if return_5 is None or rsi is None:
        return signal(context, "momentum", 0.0, "insufficient indicator history")
    score = max(-0.7, min(0.7, return_5 * 20))
    if 55 <= rsi <= 75:
        score += 0.2
    elif 25 <= rsi <= 45:
        score -= 0.2
    elif rsi > 80:
        score -= 0.2
    if volume_change is not None and volume_change > 0.25:
        score *= 1.15
    return signal(context, "momentum", score, "five-period return, RSI, and volume")
