from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from core.models import AgentType
from persistence.database import SQLiteStore


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class HealthEntry:
    component: str
    status: HealthStatus
    message: str
    checked_at: datetime
    critical: bool = True
    agent: str | None = None


class HealthRegistry:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self._store = store
        self._entries: dict[str, HealthEntry] = {}

    def set(
        self,
        component: str,
        status: HealthStatus,
        message: str,
        *,
        critical: bool = True,
        agent: AgentType | str | None = None,
        checked_at: datetime | None = None,
    ) -> HealthEntry:
        agent_value = agent.value if isinstance(agent, AgentType) else agent
        entry = HealthEntry(
            component=component,
            status=status,
            message=message,
            checked_at=(checked_at or datetime.now(UTC)).astimezone(UTC),
            critical=critical,
            agent=agent_value,
        )
        self._entries[component] = entry
        if self._store is not None:
            self._store.record("health_events", asdict(entry))
        return entry

    def get(self, component: str) -> HealthEntry | None:
        return self._entries.get(component)

    def execution_blockers(
        self,
        agent: AgentType,
        *,
        require_stream: bool,
    ) -> list[str]:
        required = {
            "database",
            "kill_switch",
            f"{agent.value}_codex_app_server",
            f"{agent.value}_codex_thread",
            f"{agent.value}_alpaca",
            f"{agent.value}_portfolio_reconciliation",
            f"{agent.value}_market_data",
        }
        if require_stream:
            required.add(f"{agent.value}_trade_stream")
        blockers: list[str] = []
        for name in sorted(required):
            entry = self._entries.get(name)
            if entry is None or entry.status is not HealthStatus.HEALTHY:
                detail = "not checked" if entry is None else entry.message
                blockers.append(f"{name}: {detail}")
        return blockers

    def order_execution_allowed(self, agent: AgentType, *, require_stream: bool = True) -> bool:
        return not self.execution_blockers(agent, require_stream=require_stream)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            name: {
                **asdict(entry),
                "status": entry.status.value,
                "checked_at": entry.checked_at.isoformat(),
            }
            for name, entry in sorted(self._entries.items())
        }

    @classmethod
    def from_store(cls, store: SQLiteStore) -> HealthRegistry:
        registry = cls()
        for row in store.records("health_events"):
            component = str(row["component"])
            registry._entries[component] = HealthEntry(
                component=component,
                status=HealthStatus(row["status"]),
                message=str(row["message"]),
                checked_at=datetime.fromisoformat(row["checked_at"]),
                critical=bool(row.get("critical", True)),
                agent=row.get("agent"),
            )
        return registry

    def human_lines(self) -> list[str]:
        if not self._entries:
            return ["No runtime health checks have been recorded."]
        lines = []
        for name, entry in sorted(self._entries.items()):
            lines.append(f"{entry.status.value.upper():9} {name}: {entry.message}")
        return lines
