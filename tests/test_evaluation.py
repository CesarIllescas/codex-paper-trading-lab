from datetime import timedelta
from decimal import Decimal

from core.models import AgentType, ExecutedTrade, OrderStatus, TradeAction
from evaluation.comparison import comparison_metrics, portfolio_metrics


def test_codex_vs_quant_comparison_metrics() -> None:
    rows = [
        {
            "codex_action": "BUY",
            "quant_action": "BUY",
            "confidence": 0.8,
            "return": 0.02,
            "codex_rejected": False,
        },
        {
            "codex_action": "HOLD",
            "quant_action": "BUY",
            "confidence": 0.4,
            "return": -0.01,
            "codex_rejected": True,
        },
    ]
    result = comparison_metrics(rows)
    assert result["agreement_rate"] == 0.5
    assert result["override_rate"] == 0.5
    assert result["hold_rate"] == 0.5
    assert result["rejected_codex_decisions"] == 1
    assert result["profitable_override_rate"] == 0.0


def test_portfolio_performance_metrics(portfolio: object, now: object) -> None:
    first = portfolio  # type: ignore[assignment]
    second = first.model_copy(  # type: ignore[attr-defined]
        update={
            "timestamp": now + timedelta(days=1),  # type: ignore[operator]
            "equity": Decimal("10100"),
            "peak_equity": Decimal("10100"),
            "realized_pnl": Decimal("100"),
            "exposure": Decimal("1000"),
        }
    )
    trade = ExecutedTrade(
        trade_id="trade",
        order_id="order",
        agent=AgentType.AGGRESSIVE_QUANT,
        symbol="BTC/USD",
        action=TradeAction.SELL,
        quantity=Decimal("1"),
        price=Decimal("100"),
        executed_at=now,
        status=OrderStatus.FILLED,
    )
    result = portfolio_metrics([first, second], [trade], initial_equity=Decimal("10000"))
    assert result["total_return"] == 0.01
    assert result["trade_count"] == 1
    assert result["win_rate"] == 1.0
