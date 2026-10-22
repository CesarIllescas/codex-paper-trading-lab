from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from core.models import Candle
from market.prices import normalize_closed_bars


@dataclass(frozen=True, slots=True)
class HistoricalDataSet:
    timeframe: str
    candles: dict[str, tuple[Candle, ...]]

    def __post_init__(self) -> None:
        if not self.candles:
            raise ValueError("historical data set cannot be empty")
        for symbol, items in self.candles.items():
            if not symbol or not items:
                raise ValueError("each historical symbol requires candles")
            timestamps = [item.timestamp for item in items]
            if timestamps != sorted(timestamps):
                raise ValueError(f"historical candles for {symbol} are not chronological")
            if len(timestamps) != len(set(timestamps)):
                raise ValueError(f"duplicate historical candles for {symbol}")
            if any(not item.is_closed for item in items):
                raise ValueError("historical replay accepts completed candles only")

    def between(self, start: datetime, end: datetime) -> HistoricalDataSet:
        start_utc = _aware_utc(start)
        end_utc = _aware_utc(end)
        if start_utc >= end_utc:
            raise ValueError("historical start must precede end")
        filtered = {
            symbol: tuple(item for item in items if start_utc <= item.timestamp < end_utc)
            for symbol, items in self.candles.items()
        }
        if any(not items for items in filtered.values()):
            raise ValueError("requested range has no candles for one or more symbols")
        return HistoricalDataSet(timeframe=self.timeframe, candles=filtered)


class HistoricalDataSource(Protocol):
    def load(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> HistoricalDataSet: ...


class InMemoryHistoricalData:
    def __init__(self, candles: Mapping[str, Iterable[Candle]]) -> None:
        self._candles = {symbol: tuple(items) for symbol, items in candles.items()}

    def load(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> HistoricalDataSet:
        missing = set(symbols) - self._candles.keys()
        if missing:
            raise ValueError(f"missing historical symbols: {', '.join(sorted(missing))}")
        selected = HistoricalDataSet(
            timeframe=timeframe,
            candles={symbol: self._candles[symbol] for symbol in symbols},
        )
        return selected.between(start, end)


class AlpacaHistoricalData:
    """Fetch historical bars from Alpaca and normalize them before replay."""

    def __init__(self, stock_client: Any, crypto_client: Any) -> None:
        self._stock_client = stock_client
        self._crypto_client = crypto_client

    def load(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> HistoricalDataSet:
        from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        start_utc = _aware_utc(start)
        end_utc = _aware_utc(end)
        if start_utc >= end_utc:
            raise ValueError("historical start must precede end")
        if timeframe == "15Min":
            alpaca_timeframe = TimeFrame(15, TimeFrameUnit.Minute)
        elif timeframe == "1Hour":
            alpaca_timeframe = TimeFrame.Hour
        else:
            raise ValueError("Phase 2 replay supports 15Min and 1Hour bars")

        crypto = [item for item in symbols if "/" in item]
        stocks = [item for item in symbols if "/" not in item]
        result: dict[str, tuple[Candle, ...]] = {}
        if crypto:
            request = CryptoBarsRequest(
                symbol_or_symbols=crypto,
                timeframe=alpaca_timeframe,
                start=start_utc,
                end=end_utc,
            )
            response = self._crypto_client.get_crypto_bars(request)
            data = getattr(response, "data", response)
            for symbol in crypto:
                result[symbol] = normalize_closed_bars(data[symbol], end_utc, timeframe)
        if stocks:
            request = StockBarsRequest(
                symbol_or_symbols=stocks,
                timeframe=alpaca_timeframe,
                start=start_utc,
                end=end_utc,
            )
            response = self._stock_client.get_stock_bars(request)
            data = getattr(response, "data", response)
            for symbol in stocks:
                result[symbol] = normalize_closed_bars(data[symbol], end_utc, timeframe)
        return HistoricalDataSet(timeframe=timeframe, candles=result).between(start_utc, end_utc)


def build_alpaca_historical_data(api_key: str, secret_key: str) -> AlpacaHistoricalData:
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient

    return AlpacaHistoricalData(
        StockHistoricalDataClient(api_key, secret_key),
        CryptoHistoricalDataClient(api_key, secret_key),
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("historical timestamps must be timezone-aware")
    return value.astimezone(UTC)
