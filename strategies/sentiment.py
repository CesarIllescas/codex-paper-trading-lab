from core.models import Signal, StrategyContext
from strategies._helpers import signal


def sentiment_signal(context: StrategyContext) -> Signal:
    """Disabled Phase 1 interface; no external sentiment is consumed."""
    return signal(context, "sentiment", 0.0, "sentiment strategy is disabled")
