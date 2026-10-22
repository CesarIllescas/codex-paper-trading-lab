from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta

from core.models import AgentType

MarketSession = tuple[datetime, datetime]


class CandleScheduler:
    """Calculate completed-candle boundaries instead of polling with fixed sleeps."""

    def __init__(self, grace_seconds: int = 5) -> None:
        if grace_seconds < 0:
            raise ValueError("scheduler grace cannot be negative")
        self.grace = timedelta(seconds=grace_seconds)

    def next_boundary(
        self,
        agent: AgentType,
        now: datetime,
        sessions: Iterable[MarketSession] = (),
    ) -> datetime:
        current = _utc(now)
        if agent is AgentType.AGGRESSIVE:
            return self._next_continuous(current, timedelta(minutes=15))
        if agent is not AgentType.CONSERVATIVE:
            raise ValueError("scheduler accepts production agents only")
        for market_open, market_close in sorted(sessions):
            opened = _utc(market_open)
            closed = _utc(market_close)
            # Alpaca's 1-hour stock bars are aligned to clock-hour boundaries,
            # including the partial opening-session bar.
            boundary = opened.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            boundary += self.grace
            while boundary <= closed + self.grace:
                if boundary > current:
                    return boundary
                boundary += timedelta(hours=1)
        raise LookupError("no future completed equity candle in supplied market sessions")

    def _next_continuous(self, now: datetime, interval: timedelta) -> datetime:
        seconds = int(interval.total_seconds())
        epoch = int(now.timestamp())
        candle_close = (epoch // seconds) * seconds
        candidate = datetime.fromtimestamp(candle_close, UTC) + self.grace
        if candidate <= now:
            candidate += interval
        return candidate

    async def run(
        self,
        next_boundary: Callable[[], datetime],
        callback: Callable[[datetime], Awaitable[None]],
        stop_event: asyncio.Event,
    ) -> None:
        while not stop_event.is_set():
            boundary = next_boundary()
            delay = max(0.0, (boundary - datetime.now(UTC)).total_seconds())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                await callback(boundary)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduler timestamps must be timezone-aware")
    return value.astimezone(UTC)
