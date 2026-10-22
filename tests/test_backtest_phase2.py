from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from backtest.execution_simulator import ExecutionAssumptions, HistoricalExecutionSimulator
from backtest.historical_data import HistoricalDataSet
from backtest.replay import ReplayEngine
from config.settings import load_settings
from core.models import (
    AgentType,
    Candle,
    OrderRequest,
    RiskDecision,
    TradeAction,
)
from portfolio.shadow import ShadowPortfolio


def candles(count: int = 65) -> tuple[Candle, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    result = []
    price = Decimal("100")
    for index in range(count):
        close = price * Decimal("1.02")
        result.append(
            Candle(
                timestamp=start + timedelta(minutes=15 * index),
                open=price,
                high=close + Decimal("1"),
                low=price - Decimal("1"),
                close=close,
                volume=Decimal("1000") + index,
            )
        )
        price = close
    return tuple(result)


def test_historical_data_rejects_bad_order_and_duplicates() -> None:
    items = candles(2)
    with pytest.raises(ValueError, match="chronological"):
        HistoricalDataSet("15Min", {"BTC/USD": tuple(reversed(items))})
    with pytest.raises(ValueError, match="duplicate"):
        HistoricalDataSet("15Min", {"BTC/USD": (items[0], items[0])})


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_has_no_lookahead() -> None:
    settings = load_settings(environ={"TRADING_ENV": "paper"})
    data = HistoricalDataSet("15Min", {"BTC/USD": candles()})
    observed = []

    async def codex_observer(market: object, signals: list, portfolio: object) -> object:
        observed.append(market)
        intent = ReplayEngine  # keep the callback fully offline and deterministic
        del signals, portfolio, intent
        from core.models import hold_intent

        return hold_intent(  # type: ignore[return-value]
            AgentType.AGGRESSIVE,
            market.symbol,  # type: ignore[attr-defined]
            market.decision_timestamp,  # type: ignore[attr-defined]
            "historical observer",
            codex_valid=True,
        )

    assumptions = ExecutionAssumptions()
    first = await ReplayEngine(
        settings,
        HistoricalExecutionSimulator(assumptions),
        codex_decider=codex_observer,  # type: ignore[arg-type]
        codex_signal_threshold=0,
    ).run(data, AgentType.AGGRESSIVE, Decimal("10000"))
    second = await ReplayEngine(settings, HistoricalExecutionSimulator(assumptions)).run(
        data, AgentType.AGGRESSIVE, Decimal("10000")
    )
    assert first.deterministic_summary() == second.deterministic_summary()
    assert observed
    assert all(
        max(bar.timestamp for bar in market.candles) < market.decision_timestamp
        for market in observed
    )


def test_execution_simulator_models_slippage_cost_and_partial_fill(now: datetime) -> None:
    portfolio = ShadowPortfolio(AgentType.AGGRESSIVE_QUANT, Decimal("1000"))
    decision = RiskDecision(
        approved=True,
        reasons=["test"],
        order_request=OrderRequest(
            agent=AgentType.AGGRESSIVE_QUANT,
            symbol="BTC/USD",
            action=TradeAction.BUY,
            quantity=Decimal("4"),
            estimated_notional=Decimal("400"),
            client_order_id="historical-order",
        ),
    )
    bar = Candle(
        timestamp=now,
        open=Decimal("100"),
        high=Decimal("102"),
        low=Decimal("99"),
        close=Decimal("101"),
        volume=Decimal("10"),
    )
    simulator = HistoricalExecutionSimulator(
        ExecutionAssumptions(
            slippage_bps=Decimal("100"),
            transaction_cost_bps=Decimal("10"),
            fill_fraction=Decimal("0.5"),
        )
    )
    trade = simulator.execute(decision, bar, portfolio, now)
    snapshot = portfolio.snapshot({"BTC/USD": Decimal("101")}, now)
    assert trade.price == Decimal("101.00")
    assert trade.quantity == Decimal("2.0")
    assert trade.status.value == "partially_filled"
    assert snapshot.positions[0].quantity == Decimal("2.0")
    assert snapshot.cash < Decimal("798")
