from __future__ import annotations

from dataclasses import dataclass

from core.models import CodexDecision

CODEX_DECISION_SCHEMA = CodexDecision.model_json_schema()


@dataclass(frozen=True, slots=True)
class CodexOutcome:
    decision: CodexDecision | None
    valid: bool
    error: str | None = None
