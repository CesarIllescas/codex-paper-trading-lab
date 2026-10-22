from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

TABLES = (
    "market_snapshots",
    "decisions",
    "codex_decisions",
    "quant_decisions",
    "strategy_signals",
    "risk_decisions",
    "orders",
    "order_updates",
    "fills",
    "positions",
    "portfolio_snapshots",
    "equity_history",
    "performance_metrics",
    "reconciliation_events",
    "health_events",
    "runtime_events",
    "backtest_runs",
    "benchmark_snapshots",
)


class SQLiteStore:
    def __init__(self, database: str | Path) -> None:
        value = str(database)
        if value.startswith("sqlite:///"):
            value = value.removeprefix("sqlite:///")
        self.path = Path(value)
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.row_factory = sqlite3.Row
        self.initialize()

    def initialize(self) -> None:
        for table in TABLES:
            self._connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("  # noqa: S608 - fixed allowlist
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "created_at TEXT NOT NULL, decision_cycle_id TEXT, payload TEXT NOT NULL)"
            )
            columns = {
                str(row["name"])
                for row in self._connection.execute(
                    f"PRAGMA table_info({table})"  # noqa: S608 - fixed allowlist
                ).fetchall()
            }
            if "decision_cycle_id" not in columns:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN decision_cycle_id TEXT"  # noqa: S608
                )
            self._connection.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_cycle "  # noqa: S608
                f"ON {table}(decision_cycle_id)"
            )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS codex_threads ("
            "agent TEXT PRIMARY KEY, thread_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._connection.commit()

    def record(
        self,
        table: str,
        value: BaseModel | dict[str, Any],
        *,
        decision_cycle_id: str | None = None,
    ) -> int:
        if table not in TABLES:
            raise ValueError(f"unsupported persistence table: {table}")
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        cursor = self._connection.execute(
            f"INSERT INTO {table} "  # noqa: S608
            "(created_at, decision_cycle_id, payload) VALUES (?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                decision_cycle_id,
                json.dumps(payload, default=str),
            ),
        )
        self._connection.commit()
        return int(cursor.lastrowid)

    def records(self, table: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unsupported persistence table: {table}")
        rows = self._connection.execute(
            f"SELECT payload FROM {table} ORDER BY id",  # noqa: S608
        ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def records_with_metadata(self, table: str) -> list[dict[str, Any]]:
        if table not in TABLES:
            raise ValueError(f"unsupported persistence table: {table}")
        rows = self._connection.execute(
            f"SELECT created_at, decision_cycle_id, payload FROM {table} ORDER BY id",  # noqa: S608
        ).fetchall()
        return [
            {
                **json.loads(row["payload"]),
                "_created_at": row["created_at"],
                "_decision_cycle_id": row["decision_cycle_id"],
            }
            for row in rows
        ]

    def cycle_records(self, decision_cycle_id: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for table in TABLES:
            rows = self._connection.execute(
                f"SELECT payload FROM {table} WHERE decision_cycle_id = ? ORDER BY id",  # noqa: S608
                (decision_cycle_id,),
            ).fetchall()
            if rows:
                result[table] = [json.loads(row["payload"]) for row in rows]
        return result

    def latest_record(self, table: str) -> dict[str, Any] | None:
        if table not in TABLES:
            raise ValueError(f"unsupported persistence table: {table}")
        row = self._connection.execute(
            f"SELECT payload FROM {table} ORDER BY id DESC LIMIT 1"  # noqa: S608
        ).fetchone()
        return None if row is None else json.loads(row["payload"])

    def ping(self) -> bool:
        try:
            return self._connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False

    def latest_portfolio_snapshot(self, portfolio_id: str) -> dict[str, Any] | None:
        rows = self._connection.execute(
            "SELECT payload FROM portfolio_snapshots ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if payload.get("portfolio_id") == portfolio_id:
                payload.pop("decision_cycle_id", None)
                return payload
        return None

    def has_recent_order(
        self,
        agent: str,
        symbol: str,
        action: str,
        window_seconds: int,
        now: datetime,
    ) -> bool:
        rows = self._connection.execute(
            "SELECT created_at, payload FROM orders ORDER BY id DESC LIMIT 100"
        ).fetchall()
        for row in rows:
            created = datetime.fromisoformat(row["created_at"])
            if (now - created).total_seconds() > window_seconds:
                break
            payload = json.loads(row["payload"])
            if (
                payload.get("agent") == agent
                and payload.get("symbol") == symbol
                and payload.get("action") == action
            ):
                return True
        return False

    def get_thread_id(self, agent: str) -> str | None:
        row = self._connection.execute(
            "SELECT thread_id FROM codex_threads WHERE agent = ?", (agent,)
        ).fetchone()
        return None if row is None else str(row["thread_id"])

    def set_thread_id(self, agent: str, thread_id: str) -> None:
        self._connection.execute(
            "INSERT INTO codex_threads (agent, thread_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(agent) DO UPDATE SET thread_id=excluded.thread_id, "
            "updated_at=excluded.updated_at",
            (agent, thread_id, datetime.now(UTC).isoformat()),
        )
        self._connection.commit()

    def delete_thread_id(self, agent: str) -> None:
        self._connection.execute("DELETE FROM codex_threads WHERE agent = ?", (agent,))
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
