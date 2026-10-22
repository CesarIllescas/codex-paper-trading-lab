from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.models import AgentType
from intelligence.codex_app_server import (
    _JSONL_STREAM_LIMIT_BYTES,
    CodexAppServer,
    CodexAppServerError,
    resolve_codex_executable,
    sanitized_environment,
)
from intelligence.codex_client import CodexClient
from intelligence.codex_threads import CodexThreadManager
from persistence.database import SQLiteStore


class FakeServer:
    def __init__(self, response: str) -> None:
        self.response = response
        self.stopped = False
        self.resumed: list[str] = []

    async def start_thread(self) -> str:
        return "thread-new"

    async def resume_thread(self, thread_id: str) -> str:
        self.resumed.append(thread_id)
        return thread_id

    async def run_turn(self, thread_id: str, text: str, schema: dict[str, object]) -> str:
        assert "quantity_instruction" in text
        assert schema["additionalProperties"] is False
        return self.response

    async def stop(self) -> None:
        self.stopped = True


def test_codex_environment_removes_brokerage_and_other_secrets() -> None:
    source = {
        "PATH": "safe",
        "USERPROFILE": "C:/user",
        "CODEX_HOME": "C:/codex",
        "ALPACA_AGGRESSIVE_API_KEY": "never",
        "ALPACA_CONSERVATIVE_SECRET_KEY": "never",
        "BINANCE_TOKEN": "never",
        "OPENAI_API_KEY": "never",
        "TRADING_ENV": "paper",
        "UNRELATED": "not allowlisted",
    }
    result = sanitized_environment(source)
    assert result == {"PATH": "safe", "USERPROFILE": "C:/user", "CODEX_HOME": "C:/codex"}
    assert all("never" not in value for value in result.values())


def test_codex_executable_falls_back_to_current_vscode_extension(tmp_path: Path) -> None:
    older = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "openai.chatgpt-1.0.0-win32-x64"
        / "bin"
        / "windows-x86_64"
        / "codex.exe"
    )
    current = (
        tmp_path
        / ".vscode"
        / "extensions"
        / "openai.chatgpt-2.0.0-win32-x64"
        / "bin"
        / "windows-x86_64"
        / "codex.exe"
    )
    older.parent.mkdir(parents=True)
    current.parent.mkdir(parents=True)
    older.write_bytes(b"older")
    current.write_bytes(b"current")
    os.utime(older, (1, 1))
    os.utime(current, (2, 2))

    resolved = resolve_codex_executable(
        "codex",
        {"PATH": str(tmp_path / "missing"), "USERPROFILE": str(tmp_path)},
        platform_name="nt",
    )

    assert resolved == str(current.resolve())


@pytest.mark.asyncio
async def test_codex_jsonl_reader_accepts_large_thread_resume_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    async def create_process(*args: object, **kwargs: object) -> None:
        del args
        captured.update(kwargs)
        raise FileNotFoundError("fake executable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    server = CodexAppServer("codex", tmp_path)
    with pytest.raises(CodexAppServerError, match="CODEX_EXECUTABLE or PATH"):
        await server.start()

    assert captured["limit"] == _JSONL_STREAM_LIMIT_BYTES
    assert _JSONL_STREAM_LIMIT_BYTES > 64 * 1024


@pytest.mark.asyncio
async def test_malformed_codex_response_means_no_decision(
    tmp_path: Path, market: object, portfolio: object
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Return structured output.", encoding="utf-8")
    store = SQLiteStore(":memory:")
    server = FakeServer("BUY BTC now")
    client = CodexClient(
        AgentType.AGGRESSIVE,
        server,  # type: ignore[arg-type]
        CodexThreadManager(store),
        prompt,
    )
    outcome = await client.decide(market, [], portfolio)  # type: ignore[arg-type]
    assert not outcome.valid
    assert outcome.decision is None
    assert server.stopped


@pytest.mark.asyncio
async def test_persistent_thread_id_is_resumed(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "threads.db")
    store.set_thread_id("aggressive", "thread-stored")
    manager = CodexThreadManager(store)
    server = FakeServer("{}")
    result = await manager.get_or_create(AgentType.AGGRESSIVE, server)
    assert result == "thread-stored"
    assert server.resumed == ["thread-stored"]
    store.close()
    reopened = SQLiteStore(tmp_path / "threads.db")
    assert reopened.get_thread_id("aggressive") == "thread-stored"


@pytest.mark.asyncio
async def test_strict_resume_failure_discards_only_stale_thread_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "strict-threads.db")
    store.set_thread_id("conservative", "thread-stale")
    manager = CodexThreadManager(store, strict_resume=True)

    class ResumeFailureServer(FakeServer):
        async def resume_thread(self, thread_id: str) -> str:
            raise RuntimeError(f"cannot resume {thread_id}")

    with pytest.raises(RuntimeError, match="cannot resume"):
        await manager.get_or_create(AgentType.CONSERVATIVE, ResumeFailureServer("{}"))

    assert store.get_thread_id("conservative") is None


@pytest.mark.asyncio
async def test_codex_timeout_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = CodexAppServer("codex", tmp_path, turn_timeout=0.001)

    async def start() -> None:
        return None

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        if method == "turn/start":
            assert params["sandboxPolicy"] == {
                "type": "readOnly",
                "networkAccess": False,
            }
            return {"turn": {"id": "turn-1"}}
        return {}

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "request", request)
    with pytest.raises(CodexAppServerError, match="timed out"):
        await server.run_turn("thread-1", "input", {"type": "object"})


@pytest.mark.asyncio
async def test_codex_thread_start_timeout_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = CodexAppServer("codex", tmp_path, startup_timeout=0.001)

    async def start() -> None:
        return None

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        del method, params
        await asyncio.Event().wait()
        return {}

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "request", request)
    with pytest.raises(CodexAppServerError, match="thread start timed out"):
        await server.start_thread()


@pytest.mark.asyncio
async def test_thread_requests_use_current_read_only_sandbox_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = CodexAppServer("codex", tmp_path)
    requests: list[tuple[str, dict[str, object]]] = []

    async def start() -> None:
        return None

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        requests.append((method, params))
        return {"thread": {"id": "thread-1"}}

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "request", request)

    assert await server.start_thread() == "thread-1"
    assert await server.resume_thread("thread-stored") == "thread-1"
    assert requests[0][0] == "thread/start"
    assert requests[0][1]["sandbox"] == "read-only"
    assert requests[0][1]["ephemeral"] is False
    assert requests[1][0] == "thread/resume"
    assert requests[1][1]["sandbox"] == "read-only"


@pytest.mark.asyncio
async def test_codex_process_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = CodexAppServer("codex", tmp_path)

    async def start() -> None:
        return None

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        return {"turn": {"id": "turn-1"}}

    monkeypatch.setattr(server, "start", start)
    monkeypatch.setattr(server, "request", request)
    await server._notifications.put({"method": "process/terminated", "params": {}})
    with pytest.raises(CodexAppServerError, match="terminated"):
        await server.run_turn("thread-1", "input", {"type": "object"})


def test_valid_codex_json_shape_is_strict() -> None:
    from pydantic import ValidationError

    from core.models import CodexDecision
    from intelligence.schemas import CODEX_DECISION_SCHEMA

    assert set(CODEX_DECISION_SCHEMA["required"]) == set(CODEX_DECISION_SCHEMA["properties"])

    payload = {
        "action": "HOLD",
        "symbol": "SPY",
        "confidence": 0.5,
        "strategy_bias": "mixed",
        "thesis": "signals conflict",
        "bull_case": [],
        "bear_case": [],
        "invalidation_conditions": [],
        "suggested_stop_loss_pct": None,
        "suggested_take_profit_pct": None,
        "time_horizon": "one day",
        "risk_flags": [],
    }
    assert CodexDecision.model_validate_json(json.dumps(payload)).action.value == "HOLD"
    payload["extra"] = "forbidden"
    with pytest.raises(ValidationError):
        CodexDecision.model_validate(payload)
