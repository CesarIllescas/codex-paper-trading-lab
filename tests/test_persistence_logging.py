import json
from pathlib import Path

from observability.jsonl import JsonlDecisionLogger
from persistence.database import SQLiteStore


def test_all_required_records_persist(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "test.db")
    for table in (
        "decisions",
        "codex_decisions",
        "strategy_signals",
        "risk_decisions",
        "orders",
        "fills",
        "positions",
        "portfolio_snapshots",
        "equity_history",
    ):
        store.record(table, {"table": table})
        assert store.records(table) == [{"table": table}]


def test_jsonl_logging_redacts_credentials(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    logger = JsonlDecisionLogger(path)
    logger.log(
        "test",
        {"symbol": "SPY", "api_key": "secret-value", "nested": {"password": "bad"}},
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["symbol"] == "SPY"
    assert "secret-value" not in path.read_text(encoding="utf-8")
    assert record["api_key"] == "[REDACTED]"
