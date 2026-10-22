from datetime import timedelta
from decimal import Decimal

import pytest

from core.models import Candle, MarketSnapshot
from market.prices import AlpacaPriceProvider, build_market_snapshot, normalize_closed_bars


def bar(timestamp: object, close: str = "100") -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "open": Decimal("100"),
        "high": Decimal("102"),
        "low": Decimal("98"),
        "close": Decimal(close),
        "volume": Decimal("10"),
    }


def test_incomplete_bar_is_excluded_and_future_data_rejected(now: object) -> None:
    closed = now - timedelta(minutes=30)  # type: ignore[operator]
    incomplete = now - timedelta(minutes=5)  # type: ignore[operator]
    result = normalize_closed_bars([bar(closed), bar(incomplete)], now, "15Min")
    assert [item.timestamp for item in result] == [closed]
    candle = Candle(**bar(closed))
    with pytest.raises(ValueError, match="look-ahead"):
        MarketSnapshot(
            symbol="SPY",
            decision_timestamp=now,
            price=Decimal("100"),
            latest_bar_at=closed,
            candles=(candle.model_copy(update={"timestamp": now + timedelta(minutes=1)}),),  # type: ignore[operator]
        )


def test_duplicate_bars_and_stale_data_are_rejected_or_flagged(now: object) -> None:
    timestamp = now - timedelta(hours=3)  # type: ignore[operator]
    with pytest.raises(ValueError, match="duplicate"):
        normalize_closed_bars([bar(timestamp), bar(timestamp)], now, "15Min")
    snapshot = build_market_snapshot("SPY", [bar(timestamp)], now, "1Hour", 120)
    assert snapshot.stale is True


def test_impossible_prices_are_rejected(now: object) -> None:
    invalid = bar(now - timedelta(hours=1))  # type: ignore[operator]
    invalid["high"] = Decimal("90")
    with pytest.raises(ValueError, match="high price"):
        normalize_closed_bars([invalid], now, "15Min")


def test_stock_market_data_explicitly_uses_iex_feed(now: object) -> None:
    from alpaca.data.enums import DataFeed

    class FakeStockClient:
        request: object | None = None

        def get_stock_bars(self, request: object) -> dict[str, list[dict[str, object]]]:
            self.request = request
            return {"SPY": [bar(now - timedelta(hours=1))]}  # type: ignore[operator]

    stock_client = FakeStockClient()
    provider = AlpacaPriceProvider(stock_client, crypto_client=None)

    snapshot = provider.get_snapshot(
        "SPY",
        timeframe="1Hour",
        stale_after_seconds=120,
        decision_timestamp=now,
    )

    assert snapshot.symbol == "SPY"
    assert stock_client.request is not None
    assert stock_client.request.feed is DataFeed.IEX  # type: ignore[union-attr]
