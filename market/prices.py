from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.models import Candle, MarketSnapshot
from market.indicators import calculate_indicators

TIMEFRAME_DURATION = {
    "1Min": timedelta(minutes=1),
    "5Min": timedelta(minutes=5),
    "15Min": timedelta(minutes=15),
    "1Hour": timedelta(hours=1),
    "1Day": timedelta(days=1),
}


def _value(bar: Any, name: str) -> Any:
    if isinstance(bar, dict):
        return bar[name]
    return getattr(bar, name)


def normalize_closed_bars(
    bars: Iterable[Any],
    decision_timestamp: datetime,
    timeframe: str,
) -> tuple[Candle, ...]:
    if decision_timestamp.tzinfo is None or decision_timestamp.utcoffset() is None:
        raise ValueError("decision timestamp must be timezone-aware")
    duration = TIMEFRAME_DURATION.get(timeframe)
    if duration is None:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    normalized: list[Candle] = []
    for bar in bars:
        timestamp = _value(bar, "timestamp")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Alpaca returned a naive timestamp")
        timestamp = timestamp.astimezone(UTC)
        if timestamp + duration > decision_timestamp.astimezone(UTC):
            continue
        normalized.append(
            Candle(
                timestamp=timestamp,
                open=Decimal(str(_value(bar, "open"))),
                high=Decimal(str(_value(bar, "high"))),
                low=Decimal(str(_value(bar, "low"))),
                close=Decimal(str(_value(bar, "close"))),
                volume=Decimal(str(_value(bar, "volume"))),
                is_closed=True,
            )
        )
    normalized.sort(key=lambda item: item.timestamp)
    timestamps = [item.timestamp for item in normalized]
    if len(timestamps) != len(set(timestamps)):
        raise ValueError("duplicate market-data candles")
    return tuple(normalized)


def build_market_snapshot(
    symbol: str,
    bars: Iterable[Any],
    decision_timestamp: datetime,
    timeframe: str,
    stale_after_seconds: int,
) -> MarketSnapshot:
    closed = normalize_closed_bars(bars, decision_timestamp, timeframe)
    if not closed:
        raise ValueError("no closed candles are available")
    latest = closed[-1]
    stale = (decision_timestamp.astimezone(UTC) - latest.timestamp).total_seconds() > (
        stale_after_seconds + TIMEFRAME_DURATION[timeframe].total_seconds()
    )
    return MarketSnapshot(
        symbol=symbol,
        decision_timestamp=decision_timestamp.astimezone(UTC),
        price=latest.close,
        latest_bar_at=latest.timestamp,
        candles=closed,
        indicators=calculate_indicators(closed),
        stale=stale,
    )


class AlpacaPriceProvider:
    """Fetch Alpaca bars and immediately normalize them into internal models."""

    def __init__(self, stock_client: Any, crypto_client: Any) -> None:
        self._stock_client = stock_client
        self._crypto_client = crypto_client

    def get_snapshot(
        self,
        symbol: str,
        *,
        timeframe: str,
        stale_after_seconds: int,
        decision_timestamp: datetime | None = None,
        lookback_bars: int = 120,
    ) -> MarketSnapshot:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        now = (decision_timestamp or datetime.now(UTC)).astimezone(UTC)
        duration = TIMEFRAME_DURATION[timeframe]
        amount = int(duration.total_seconds() // 60)
        if timeframe == "1Hour":
            alpaca_timeframe = TimeFrame.Hour
        elif timeframe == "1Day":
            alpaca_timeframe = TimeFrame.Day
        else:
            alpaca_timeframe = TimeFrame(amount, TimeFrameUnit.Minute)
        start = now - duration * (lookback_bars + 2)
        if "/" in symbol:
            request = CryptoBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=alpaca_timeframe,
                start=start,
                end=now,
            )
            response = self._crypto_client.get_crypto_bars(request)
        else:
            request = StockBarsRequest(
                symbol_or_symbols=[symbol],
                timeframe=alpaca_timeframe,
                start=start,
                end=now,
                feed=DataFeed.IEX,
            )
            response = self._stock_client.get_stock_bars(request)
        data = getattr(response, "data", response)
        return build_market_snapshot(
            symbol,
            data[symbol],
            now,
            timeframe,
            stale_after_seconds,
        )


def build_alpaca_price_provider(api_key: str, secret_key: str) -> AlpacaPriceProvider:
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

    return AlpacaPriceProvider(
        stock_client=StockHistoricalDataClient(api_key, secret_key),
        crypto_client=CryptoHistoricalDataClient(api_key, secret_key),
    )
