from __future__ import annotations

import math
from collections import defaultdict
from decimal import Decimal
from typing import Any

import numpy as np

from core.models import ExecutedTrade, PortfolioSnapshot, TradeAction


def portfolio_metrics(
    snapshots: list[PortfolioSnapshot],
    trades: list[ExecutedTrade],
    *,
    initial_equity: Decimal,
) -> dict[str, float | int | None]:
    if not snapshots:
        return {
            "initial_capital": float(initial_equity),
            "current_equity": float(initial_equity),
            "total_return": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "max_drawdown": 0.0,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "trade_count": len(trades),
            "win_rate": None,
            "average_winner": None,
            "average_loser": None,
            "profit_factor": None,
            "exposure": 0.0,
            "turnover": 0.0,
        }
    equities = np.array([float(item.equity) for item in snapshots], dtype=float)
    returns = np.diff(equities) / equities[:-1] if len(equities) > 1 else np.array([])
    sharpe = None
    if len(returns) > 1 and float(np.std(returns, ddof=1)) > 0:
        sharpe = float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(252))
    downside = returns[returns < 0]
    sortino = None
    if len(downside) > 1 and float(np.std(downside, ddof=1)) > 0:
        sortino = float(np.mean(returns) / np.std(downside, ddof=1) * math.sqrt(252))
    # Round-trip outcome is represented by realized P&L changes in snapshots.
    pnl_changes = np.diff([0.0, *[float(item.realized_pnl) for item in snapshots]])
    outcomes = pnl_changes[pnl_changes != 0]
    winners = outcomes[outcomes > 0]
    losers = outcomes[outcomes < 0]
    turnover = sum(float(item.quantity * item.price) for item in trades) / float(initial_equity)
    latest = snapshots[-1]
    return {
        "initial_capital": float(initial_equity),
        "current_equity": float(latest.equity),
        "total_return": float((latest.equity - initial_equity) / initial_equity),
        "realized_pnl": float(latest.realized_pnl),
        "unrealized_pnl": float(latest.unrealized_pnl),
        "max_drawdown": max(float(item.drawdown_pct) for item in snapshots),
        "sharpe_ratio": sharpe,
        "sortino_ratio": sortino,
        "trade_count": len(trades),
        "win_rate": None if len(outcomes) == 0 else float(len(winners) / len(outcomes)),
        "average_winner": None if len(winners) == 0 else float(np.mean(winners)),
        "average_loser": None if len(losers) == 0 else float(np.mean(losers)),
        "profit_factor": (None if len(losers) == 0 else float(winners.sum() / abs(losers.sum()))),
        "exposure": float(latest.exposure / latest.equity),
        "turnover": turnover,
    }


def comparison_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "agreement_rate": None,
            "override_rate": None,
            "return_when_agrees": None,
            "return_when_overrides": None,
            "profitable_override_rate": None,
            "rejected_codex_decisions": 0,
            "codex_failures": 0,
            "hold_rate": None,
            "average_confidence": None,
            "performance_by_confidence": {},
            "sample_size": 0,
        }
    agreements = [row["codex_action"] == row["quant_action"] for row in rows]
    agreement_returns = [
        float(row["return"])
        for row, same in zip(rows, agreements, strict=True)
        if same and row.get("return") is not None
    ]
    override_returns = [
        float(row["return"])
        for row, same in zip(rows, agreements, strict=True)
        if not same and row.get("return") is not None
    ]
    buckets: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        confidence = float(row.get("confidence", 0))
        lower = min(int(confidence * 10) * 10, 90)
        if row.get("return") is not None:
            buckets[f"{lower / 100:.1f}-{(lower + 10) / 100:.1f}"].append(float(row["return"]))
    return {
        "agreement_rate": sum(agreements) / len(rows),
        "override_rate": 1 - (sum(agreements) / len(rows)),
        "return_when_agrees": (
            None if not agreement_returns else sum(agreement_returns) / len(agreement_returns)
        ),
        "return_when_overrides": (
            None if not override_returns else sum(override_returns) / len(override_returns)
        ),
        "profitable_override_rate": (
            None
            if not override_returns
            else sum(value > 0 for value in override_returns) / len(override_returns)
        ),
        "rejected_codex_decisions": sum(bool(row.get("codex_rejected")) for row in rows),
        "codex_failures": sum(not bool(row.get("codex_valid", True)) for row in rows),
        "hold_rate": sum(row["codex_action"] == TradeAction.HOLD.value for row in rows) / len(rows),
        "average_confidence": sum(float(row.get("confidence", 0)) for row in rows) / len(rows),
        "performance_by_confidence": {
            key: {"mean_return": sum(values) / len(values), "count": len(values)}
            for key, values in sorted(buckets.items())
        },
        "sample_size": len(rows),
    }
