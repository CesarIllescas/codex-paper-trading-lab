from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from itertools import groupby

from backtest.execution_simulator import HistoricalExecutionSimulator
from backtest.historical_data import HistoricalDataSet
from config.settings import Settings
from core.models import (
    AgentType,
    Candle,
    ExecutedTrade,
    MarketSnapshot,
    PortfolioSnapshot,
    RiskDecision,
    TradeAction,
    TradeIntent,
)
from market.indicators import calculate_indicators
from market.prices import TIMEFRAME_DURATION
from portfolio.shadow import HistoricalPortfolio, ShadowPortfolio
from risk import AggressiveRiskEngine, ConservativeRiskEngine
from strategies.baseline import deterministic_baseline
from strategies.pipeline import generate_signals, strategy_weights

CodexReplayDecider = Callable[[MarketSnapshot, list, PortfolioSnapshot], Awaitable[TradeIntent]]


@dataclass(slots=True)
class ReplayResult:
    agent: AgentType
    snapshots: list[PortfolioSnapshot] = field(default_factory=list)
    decisions: list[TradeIntent] = field(default_factory=list)
    risk_decisions: list[RiskDecision] = field(default_factory=list)
    trades: list[ExecutedTrade] = field(default_factory=list)
    codex_decisions: list[TradeIntent] = field(default_factory=list)
    codex_snapshots: list[PortfolioSnapshot] = field(default_factory=list)
    codex_risk_decisions: list[RiskDecision] = field(default_factory=list)
    codex_trades: list[ExecutedTrade] = field(default_factory=list)
    codex_invocations: int = 0
    benchmark_return: Decimal | None = None

    def deterministic_summary(self) -> dict[str, object]:
        final = self.snapshots[-1]
        return {
            "agent": self.agent.value,
            "final_equity": str(final.equity),
            "realized_pnl": str(final.realized_pnl),
            "trade_count": len(self.trades),
            "actions": [item.action.value for item in self.decisions],
            "benchmark_return": None
            if self.benchmark_return is None
            else str(self.benchmark_return),
        }


class ReplayEngine:
    """Replay completed bars chronologically with a next-bar execution model."""

    def __init__(
        self,
        settings: Settings,
        simulator: HistoricalExecutionSimulator,
        *,
        codex_decider: CodexReplayDecider | None = None,
        codex_signal_threshold: float = 0.65,
    ) -> None:
        self.settings = settings
        self.simulator = simulator
        self.codex_decider = codex_decider
        self.codex_signal_threshold = codex_signal_threshold

    async def run(
        self,
        data: HistoricalDataSet,
        agent: AgentType,
        initial_capital: Decimal,
        *,
        decision_start: datetime | None = None,
    ) -> ReplayResult:
        base_agent, quant_agent = _agents(agent)
        expected_timeframe = getattr(self.settings.market, base_agent.value).timeframe
        if data.timeframe != expected_timeframe:
            raise ValueError(f"{base_agent.value} replay requires {expected_timeframe} bars")
        portfolio = ShadowPortfolio(quant_agent, initial_capital)
        codex_portfolio = (
            HistoricalPortfolio(base_agent, initial_capital)
            if self.codex_decider is not None
            else None
        )
        result = ReplayResult(agent=quant_agent)
        history: dict[str, list[Candle]] = defaultdict(list)
        last_prices: dict[str, Decimal] = {}
        pending: dict[str, RiskDecision] = {}
        codex_pending: dict[str, RiskDecision] = {}
        duration = TIMEFRAME_DURATION[data.timeframe]
        symbol_order = {symbol: index for index, symbol in enumerate(data.candles)}
        events = sorted(
            [
                (bar.timestamp + duration, symbol, bar)
                for symbol, bars in data.candles.items()
                for bar in bars
            ],
            key=lambda item: (item[0], symbol_order[item[1]]),
        )
        risk_engine = (
            AggressiveRiskEngine(self.settings.agents[base_agent.value].risk)
            if base_agent is AgentType.AGGRESSIVE
            else ConservativeRiskEngine(self.settings.agents[base_agent.value].risk)
        )
        weights = strategy_weights(self.settings, base_agent)
        start_at = None if decision_start is None else decision_start.astimezone(UTC)
        first_prices = {
            symbol: next(
                (
                    bar.close
                    for bar in bars
                    if start_at is None or bar.timestamp + duration >= start_at
                ),
                bars[-1].close,
            )
            for symbol, bars in data.candles.items()
        }
        last_benchmark_prices = dict(first_prices)

        for decision_at, event_group in groupby(events, key=lambda item: item[0]):
            batch = list(event_group)
            # All orders at timestamp T fill before any T-close decision is evaluated.
            for _, symbol, bar in batch:
                if symbol in pending:
                    trade = self.simulator.execute(
                        pending.pop(symbol),
                        bar,
                        portfolio,
                        bar.timestamp,
                    )
                    result.trades.append(trade)
                if codex_portfolio is not None and symbol in codex_pending:
                    codex_trade = self.simulator.execute(
                        codex_pending.pop(symbol),
                        bar,
                        codex_portfolio,
                        bar.timestamp,
                    )
                    result.codex_trades.append(codex_trade)
            for _, symbol, bar in batch:
                history[symbol].append(bar)
                last_prices[symbol] = bar.close
                last_benchmark_prices[symbol] = bar.close
            for _, symbol, bar in batch:
                if len(history[symbol]) < self.settings.backtest.warmup_bars or (
                    start_at is not None and decision_at < start_at
                ):
                    continue
                candles = tuple(history[symbol])
                market = MarketSnapshot(
                    symbol=symbol,
                    decision_timestamp=decision_at.astimezone(UTC),
                    price=bar.close,
                    latest_bar_at=bar.timestamp,
                    candles=candles,
                    indicators=calculate_indicators(candles),
                    stale=False,
                )
                snapshot = portfolio.snapshot(last_prices, decision_at)
                signals = generate_signals(self.settings, quant_agent, market)
                intent = deterministic_baseline(quant_agent, signals, weights)
                result.decisions.append(intent)
                decision = risk_engine.evaluate(intent, snapshot, market)
                result.risk_decisions.append(decision)
                if decision.approved:
                    pending[symbol] = decision

                if (
                    self.codex_decider is not None
                    and intent.action is not TradeAction.HOLD
                    and intent.confidence >= self.codex_signal_threshold
                ):
                    result.codex_invocations += 1
                    assert codex_portfolio is not None
                    codex_snapshot = codex_portfolio.snapshot(last_prices, decision_at)
                    codex_intent = await self.codex_decider(market, signals, codex_snapshot)
                    result.codex_decisions.append(codex_intent)
                    codex_risk = risk_engine.evaluate(codex_intent, codex_snapshot, market)
                    result.codex_risk_decisions.append(codex_risk)
                    if codex_risk.approved:
                        codex_pending[symbol] = codex_risk
                if codex_portfolio is not None:
                    result.codex_snapshots.append(
                        codex_portfolio.snapshot(last_prices, decision_at)
                    )
                result.snapshots.append(portfolio.snapshot(last_prices, decision_at))

        if result.snapshots:
            benchmark_symbols = ["SPY"] if "SPY" in first_prices else list(first_prices)
            benchmark_returns = [
                (last_benchmark_prices[symbol] - first_prices[symbol]) / first_prices[symbol]
                for symbol in benchmark_symbols
            ]
            result.benchmark_return = sum(benchmark_returns, Decimal("0")) / Decimal(
                len(benchmark_returns)
            )
        else:
            raise ValueError("insufficient historical bars after warmup")
        return result


def _agents(agent: AgentType) -> tuple[AgentType, AgentType]:
    if agent in {AgentType.AGGRESSIVE, AgentType.AGGRESSIVE_QUANT}:
        return AgentType.AGGRESSIVE, AgentType.AGGRESSIVE_QUANT
    if agent in {AgentType.CONSERVATIVE, AgentType.CONSERVATIVE_QUANT}:
        return AgentType.CONSERVATIVE, AgentType.CONSERVATIVE_QUANT
    raise ValueError("unsupported replay agent")
