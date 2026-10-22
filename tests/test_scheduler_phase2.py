from datetime import UTC, datetime

import pytest

from core.models import AgentType
from runtime.scheduler import CandleScheduler


def test_crypto_scheduler_uses_completed_fifteen_minute_boundaries() -> None:
    scheduler = CandleScheduler(grace_seconds=5)
    now = datetime(2026, 1, 1, 12, 0, 3, tzinfo=UTC)
    assert scheduler.next_boundary(AgentType.AGGRESSIVE, now) == datetime(
        2026, 1, 1, 12, 0, 5, tzinfo=UTC
    )
    after_grace = datetime(2026, 1, 1, 12, 0, 6, tzinfo=UTC)
    assert scheduler.next_boundary(AgentType.AGGRESSIVE, after_grace) == datetime(
        2026, 1, 1, 12, 15, 5, tzinfo=UTC
    )


def test_equity_scheduler_stays_inside_market_sessions() -> None:
    scheduler = CandleScheduler(grace_seconds=5)
    session = (
        datetime(2026, 1, 2, 14, 30, tzinfo=UTC),
        datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
    )
    result = scheduler.next_boundary(
        AgentType.CONSERVATIVE,
        datetime(2026, 1, 2, 14, 45, tzinfo=UTC),
        [session],
    )
    assert result == datetime(2026, 1, 2, 15, 0, 5, tzinfo=UTC)
    assert scheduler.next_boundary(
        AgentType.CONSERVATIVE,
        datetime(2026, 1, 2, 15, 0, 6, tzinfo=UTC),
        [session],
    ) == datetime(2026, 1, 2, 16, 0, 5, tzinfo=UTC)
    with pytest.raises(LookupError):
        scheduler.next_boundary(
            AgentType.CONSERVATIVE,
            datetime(2026, 1, 2, 22, tzinfo=UTC),
            [session],
        )
