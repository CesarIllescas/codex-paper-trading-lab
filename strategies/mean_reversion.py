from core.models import Signal, StrategyContext
from strategies._helpers import number, signal


def mean_reversion_signal(context: StrategyContext) -> Signal:
    sma_20 = number(context, "sma_20")
    rsi = number(context, "rsi_14")
    if sma_20 is None or rsi is None or sma_20 <= 0:
        return signal(context, "mean_reversion", 0.0, "insufficient indicator history")
    deviation = (float(context.market.price) - sma_20) / sma_20
    score = -max(-0.7, min(0.7, deviation * 12))
    if rsi < 30:
        score += 0.3
    elif rsi > 70:
        score -= 0.3
    return signal(context, "mean_reversion", score, "distance from SMA20 and RSI extremes")
