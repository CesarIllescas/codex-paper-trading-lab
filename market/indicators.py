from __future__ import annotations

import numpy as np
import pandas as pd

from core.models import Candle


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + relative_strength))
    return result.where(loss != 0, 100.0).where(gain != 0, 0.0)


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    line = ema(series, fast) - ema(series, slow)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, signal_line, line - signal_line


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def percentage_return(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods=periods, fill_method=None)


def rolling_volatility(series: pd.Series, period: int = 20) -> pd.Series:
    return percentage_return(series).rolling(period, min_periods=period).std(ddof=0)


def volume_change(series: pd.Series, periods: int = 1) -> pd.Series:
    return series.pct_change(periods=periods, fill_method=None)


def candles_to_frame(candles: tuple[Candle, ...] | list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": candle.timestamp,
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": float(candle.volume),
            }
            for candle in candles
        ]
    ).set_index("timestamp")


def calculate_indicators(candles: tuple[Candle, ...] | list[Candle]) -> dict[str, float | None]:
    if not candles:
        return {}
    frame = candles_to_frame(candles)
    close = frame["close"]
    macd_line, macd_signal, macd_histogram = macd(close)
    values = {
        "sma_20": sma(close, 20).iloc[-1],
        "sma_50": sma(close, 50).iloc[-1],
        "ema_12": ema(close, 12).iloc[-1],
        "ema_20": ema(close, 20).iloc[-1],
        "ema_50": ema(close, 50).iloc[-1],
        "rsi_14": rsi(close, 14).iloc[-1],
        "macd": macd_line.iloc[-1],
        "macd_signal": macd_signal.iloc[-1],
        "macd_histogram": macd_histogram.iloc[-1],
        "atr_14": atr(frame, 14).iloc[-1],
        "return_1": percentage_return(close, 1).iloc[-1],
        "return_5": percentage_return(close, 5).iloc[-1],
        "rolling_volatility_20": rolling_volatility(close, 20).iloc[-1],
        "volume_change": volume_change(frame["volume"], 1).iloc[-1],
    }
    return {key: None if pd.isna(value) else float(value) for key, value in values.items()}
