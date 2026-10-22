from core.models import Signal, StrategyContext
from strategies._helpers import number, signal


def trend_following_signal(context: StrategyContext) -> Signal:
    ema_20 = number(context, "ema_20")
    ema_50 = number(context, "ema_50")
    histogram = number(context, "macd_histogram")
    if ema_20 is None or ema_50 is None or histogram is None or ema_50 <= 0:
        return signal(context, "trend_following", 0.0, "insufficient indicator history")
    spread = (ema_20 - ema_50) / ema_50
    score = max(-0.75, min(0.75, spread * 30))
    if histogram > 0:
        score += 0.2
    elif histogram < 0:
        score -= 0.2
    return signal(context, "trend_following", score, "EMA20/EMA50 trend and MACD confirmation")
