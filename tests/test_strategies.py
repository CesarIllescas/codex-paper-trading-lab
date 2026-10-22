from datetime import timedelta
from decimal import Decimal

from core.models import AgentType, Candle, MarketSnapshot, Signal, StrategyContext, TradeAction
from market.indicators import calculate_indicators
from strategies import mean_reversion_signal, momentum_signal, trend_following_signal
from strategies.baseline import deterministic_baseline


def test_indicators_and_strategies_are_deterministic(now: object) -> None:
    candles = tuple(
        Candle(
            timestamp=now - timedelta(minutes=15 * (60 - index)),  # type: ignore[operator]
            open=Decimal(100 + index),
            high=Decimal(102 + index),
            low=Decimal(99 + index),
            close=Decimal(101 + index),
            volume=Decimal(1000 + index * 10),
        )
        for index in range(60)
    )
    indicators = calculate_indicators(candles)
    market = MarketSnapshot(
        symbol="BTC/USD",
        decision_timestamp=now,
        price=candles[-1].close,
        latest_bar_at=candles[-1].timestamp,
        candles=candles,
        indicators=indicators,
    )
    context = StrategyContext(
        agent=AgentType.AGGRESSIVE, market=market, indicator_values=indicators
    )
    first = [
        momentum_signal(context),
        mean_reversion_signal(context),
        trend_following_signal(context),
    ]
    second = [
        momentum_signal(context),
        mean_reversion_signal(context),
        trend_following_signal(context),
    ]
    assert first == second


def test_baseline_uses_weighted_signals_only(now: object) -> None:
    signals = [
        Signal(
            strategy="momentum",
            symbol="BTC/USD",
            action=TradeAction.BUY,
            score=0.8,
            confidence=0.8,
            timestamp=now,
            rationale="test",
        ),
        Signal(
            strategy="trend_following",
            symbol="BTC/USD",
            action=TradeAction.HOLD,
            score=0.0,
            confidence=0.0,
            timestamp=now,
            rationale="test",
        ),
    ]
    result = deterministic_baseline(
        AgentType.AGGRESSIVE_QUANT, signals, {"momentum": 0.4, "trend_following": 0.6}
    )
    assert result.action is TradeAction.BUY
    assert "Codex" not in result.thesis
