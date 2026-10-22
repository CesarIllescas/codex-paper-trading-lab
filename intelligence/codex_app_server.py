from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class CodexAppServerError(RuntimeError):
    pass


_JSONL_STREAM_LIMIT_BYTES = 16 * 1024 * 1024


_SAFE_ENVIRONMENT_KEYS = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "CODEX_HOME",
    "COLORTERM",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LOCALAPPDATA",
    "NO_COLOR",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PROGRAMW6432",
    "PSMODULEPATH",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}
_FINANCIAL_OR_SECRET = re.compile(
    r"(ALPACA|BROKER|BINANCE|COINBASE|IBKR|TRADING.*(KEY|SECRET)|API_KEY|SECRET_KEY|TOKEN|PASSWORD)",
    re.IGNORECASE,
)


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a minimal allowlisted environment; never clone the parent wholesale."""
    values = os.environ if source is None else source
    result = {
        key: value
        for key, value in values.items()
        if key.upper() in _SAFE_ENVIRONMENT_KEYS and not _FINANCIAL_OR_SECRET.search(key)
    }
    # Defense in depth for the exact project credentials.
    for key in (
        "ALPACA_AGGRESSIVE_API_KEY",
        "ALPACA_AGGRESSIVE_SECRET_KEY",
        "ALPACA_CONSERVATIVE_API_KEY",
        "ALPACA_CONSERVATIVE_SECRET_KEY",
    ):
        result.pop(key, None)
    return result


def resolve_codex_executable(
    executable: str,
    environment: Mapping[str, str],
    *,
    platform_name: str | None = None,
) -> str | None:
    """Resolve Codex without depending on a versioned VS Code PATH entry."""

    configured = Path(executable).expanduser()
    is_explicit_path = configured.is_absolute() or configured.parent != Path(".")
    if is_explicit_path and configured.is_file():
        return str(configured.resolve())

    if not is_explicit_path:
        discovered = shutil.which(executable, path=environment.get("PATH"))
        if discovered:
            return str(Path(discovered).resolve())

    platform_name = os.name if platform_name is None else platform_name
    if platform_name != "nt" or configured.name.lower() not in {"codex", "codex.exe"}:
        return None

    user_profile = environment.get("USERPROFILE")
    if not user_profile:
        return None
    extension_roots = (
        Path(user_profile) / ".vscode" / "extensions",
        Path(user_profile) / ".vscode-insiders" / "extensions",
    )
    candidates: list[Path] = []
    for root in extension_roots:
        candidates.extend(
            candidate.resolve()
            for candidate in root.glob("openai.chatgpt-*-win32-*/bin/windows-*/codex.exe")
            if candidate.is_file() and candidate.resolve().is_relative_to(root.resolve())
        )
    if not candidates:
        return None
    return str(max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns))


class CodexAppServer:
    def __init__(
        self,
        executable: str,
        runtime_directory: Path,
        *,
        startup_timeout: float = 30,
        turn_timeout: float = 120,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = executable
        self.runtime_directory = runtime_directory.resolve()
        self.startup_timeout = startup_timeout
        self.turn_timeout = turn_timeout
        self.environment = sanitized_environment(environment)
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._write_lock = asyncio.Lock()
        self._next_id = 1

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        self.runtime_directory.mkdir(parents=True, exist_ok=True)
        if any(self.runtime_directory.rglob(".env*")):
            raise CodexAppServerError("Codex runtime directory contains a forbidden .env file")
        self._notifications = asyncio.Queue()
        resolved_executable = resolve_codex_executable(self.executable, self.environment)
        if resolved_executable is None:
            raise CodexAppServerError(
                "Codex executable is unavailable; check CODEX_EXECUTABLE or PATH"
            )
        try:
            self._process = await asyncio.create_subprocess_exec(
                resolved_executable,
                "app-server",
                "--listen",
                "stdio://",
                cwd=self.runtime_directory,
                env=self.environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # thread/resume may return contextual history in one JSONL record.
                # Keep this bounded, but well above asyncio's 64 KiB default.
                limit=_JSONL_STREAM_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise CodexAppServerError(
                "Codex executable is unavailable; check CODEX_EXECUTABLE or PATH"
            ) from exc
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        try:
            await asyncio.wait_for(
                self.request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "ai_trading_paper_experiment",
                            "title": "AI Trading Paper Experiment",
                            "version": "0.2.0",
                        }
                    },
                ),
                timeout=self.startup_timeout,
            )
            await self.notify("initialized", {})
        except Exception:
            await self.stop()
            raise

    async def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        process = self._process
        read_error: CodexAppServerError | None = None
        try:
            while line := await process.stdout.readline():
                try:
                    message: dict[str, Any] = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                message_id = message.get("id")
                if message_id in self._pending:
                    future = self._pending.pop(message_id)
                    if "error" in message:
                        future.set_exception(CodexAppServerError(str(message["error"])))
                    else:
                        future.set_result(message.get("result", {}))
                elif message_id is not None and "method" in message:
                    await self._send(
                        {
                            "id": message_id,
                            "error": {"code": -32601, "message": "Client method not supported"},
                        }
                    )
                else:
                    await self._notifications.put(message)
        except (OSError, RuntimeError, ValueError) as exc:
            read_error = CodexAppServerError(
                f"Codex App Server protocol reader failed: {type(exc).__name__}"
            )
        finally:
            error = read_error or CodexAppServerError("Codex App Server process terminated")
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(error)
            self._pending.clear()
            if self._process is process:
                await self._notifications.put({"method": "process/terminated", "params": {}})

    async def _discard_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while await self._process.stderr.readline():
            pass

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.running or self._process is None or self._process.stdin is None:
            raise CodexAppServerError("Codex App Server is not running")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        async with self._write_lock:
            self._process.stdin.write(payload)
            await self._process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"method": method, "id": request_id, "params": params})
            return await future
        except Exception:
            self._pending.pop(request_id, None)
            raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def start_thread(self) -> str:
        await self.start()
        try:
            result = await asyncio.wait_for(
                self.request(
                    "thread/start",
                    {
                        "cwd": str(self.runtime_directory),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                        "ephemeral": False,
                        "serviceName": "ai_trading_paper_experiment",
                    },
                ),
                timeout=self.startup_timeout,
            )
        except TimeoutError as exc:
            raise CodexAppServerError("Codex thread start timed out") from exc
        return str(result["thread"]["id"])

    async def resume_thread(self, thread_id: str) -> str:
        await self.start()
        try:
            result = await asyncio.wait_for(
                self.request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "cwd": str(self.runtime_directory),
                        "approvalPolicy": "never",
                        "sandbox": "read-only",
                    },
                ),
                timeout=self.startup_timeout,
            )
        except TimeoutError as exc:
            raise CodexAppServerError("Codex thread resume timed out") from exc
        return str(result["thread"]["id"])

    async def run_turn(
        self,
        thread_id: str,
        input_text: str,
        output_schema: dict[str, Any],
    ) -> str:
        await self.start()
        try:
            result = await asyncio.wait_for(
                self.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": input_text}],
                        "cwd": str(self.runtime_directory),
                        "approvalPolicy": "never",
                        "sandboxPolicy": {
                            "type": "readOnly",
                            "networkAccess": False,
                        },
                        "outputSchema": output_schema,
                    },
                ),
                timeout=self.startup_timeout,
            )
            turn_id = str(result["turn"]["id"])
            final_text: str | None = None
            deadline = asyncio.get_running_loop().time() + self.turn_timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                message = await asyncio.wait_for(self._notifications.get(), timeout=remaining)
                method = message.get("method")
                params = message.get("params", {})
                if method == "process/terminated":
                    raise CodexAppServerError("Codex App Server terminated during turn")
                if method == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("phase") in {
                        None,
                        "final_answer",
                    }:
                        final_text = item.get("text")
                if method == "turn/completed":
                    turn = params.get("turn", {})
                    if str(turn.get("id")) != turn_id:
                        continue
                    if turn.get("status") != "completed" or final_text is None:
                        raise CodexAppServerError(f"Codex turn failed: {turn.get('error')}")
                    return final_text
        except TimeoutError as exc:
            try:
                if "turn_id" in locals():
                    await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
            except Exception:
                pass
            raise CodexAppServerError("Codex turn timed out") from exc

    async def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        tasks = [task for task in (self._reader_task, self._stderr_task) if task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
