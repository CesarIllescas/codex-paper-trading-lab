from __future__ import annotations

from typing import Any

PORTFOLIO_LABELS = {
    "aggressive_codex": "Aggressive Codex",
    "aggressive_quant": "Aggressive Quant",
    "conservative_codex": "Conservative Codex",
    "conservative_quant": "Conservative Quant",
    "passive_benchmark": "Passive Benchmark",
}


def comparison_table(report: dict[str, Any]) -> str:
    columns = (
        ("Portfolio", 21),
        ("Initial", 11),
        ("Equity", 11),
        ("Return", 9),
        ("Realized", 11),
        ("Unrealized", 11),
        ("Max DD", 8),
        ("Sharpe", 8),
        ("Sortino", 8),
        ("Trades", 7),
        ("Win %", 7),
        ("PF", 7),
        ("Exposure", 9),
    )
    header = " ".join(label.ljust(width) for label, width in columns)
    lines = [header, "-" * len(header)]
    for portfolio_id, label in PORTFOLIO_LABELS.items():
        metrics = report.get(portfolio_id, {})
        row = [
            label.ljust(21),
            _number(metrics.get("initial_capital"), 11),
            _number(metrics.get("current_equity"), 11),
            _percent(metrics.get("total_return"), 9),
            _number(metrics.get("realized_pnl"), 11),
            _number(metrics.get("unrealized_pnl"), 11),
            _percent(metrics.get("max_drawdown"), 8),
            _ratio(metrics.get("sharpe_ratio"), 8),
            _ratio(metrics.get("sortino_ratio"), 8),
            str(metrics.get("trade_count", 0)).rjust(7),
            _percent(metrics.get("win_rate"), 7),
            _ratio(metrics.get("profit_factor"), 7),
            _percent(metrics.get("exposure"), 9),
        ]
        lines.append(" ".join(row))
    comparisons = report.get("codex_vs_quant", {})
    for agent in ("aggressive", "conservative"):
        values = comparisons.get(agent, {})
        lines.extend(
            [
                "",
                f"{agent.title()} Codex contribution "
                f"(n={values.get('sample_size', 0)}): "
                f"agreement={_inline_percent(values.get('agreement_rate'))}, "
                f"override={_inline_percent(values.get('override_rate'))}, "
                f"profitable overrides={_inline_percent(values.get('profitable_override_rate'))}, "
                f"HOLD={_inline_percent(values.get('hold_rate'))}, "
                f"avg confidence={_inline_percent(values.get('average_confidence'))}, "
                f"Codex failures={values.get('codex_failures', 0)}",
                "  performance after agreement="
                f"{_inline_percent(values.get('return_when_agrees'))}; "
                f"after override={_inline_percent(values.get('return_when_overrides'))}; "
                f"confidence buckets={values.get('performance_by_confidence', {})}",
            ]
        )
    return "\n".join(lines)


def _number(value: Any, width: int) -> str:
    return ("-" if value is None else f"{float(value):,.2f}").rjust(width)


def _percent(value: Any, width: int) -> str:
    return ("-" if value is None else f"{float(value) * 100:.2f}%").rjust(width)


def _ratio(value: Any, width: int) -> str:
    return ("-" if value is None else f"{float(value):.2f}").rjust(width)


def _inline_percent(value: Any) -> str:
    return "-" if value is None else f"{float(value) * 100:.2f}%"
