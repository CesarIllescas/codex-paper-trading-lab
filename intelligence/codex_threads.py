from __future__ import annotations

from typing import Protocol

from core.models import AgentType


class ThreadPersistence(Protocol):
    def get_thread_id(self, agent: str) -> str | None: ...

    def set_thread_id(self, agent: str, thread_id: str) -> None: ...

    def delete_thread_id(self, agent: str) -> None: ...


class ThreadProtocol(Protocol):
    async def start_thread(self) -> str: ...

    async def resume_thread(self, thread_id: str) -> str: ...


class CodexThreadManager:
    def __init__(self, persistence: ThreadPersistence, *, strict_resume: bool = False) -> None:
        self._persistence = persistence
        self._strict_resume = strict_resume
        self._loaded: dict[AgentType, str] = {}

    async def get_or_create(self, agent: AgentType, server: ThreadProtocol) -> str:
        if agent in self._loaded:
            return self._loaded[agent]
        stored = self._persistence.get_thread_id(agent.value)
        if stored:
            try:
                thread_id = await server.resume_thread(stored)
            except Exception:
                if self._strict_resume:
                    self._persistence.delete_thread_id(agent.value)
                    raise
                thread_id = await server.start_thread()
        else:
            thread_id = await server.start_thread()
        self._persistence.set_thread_id(agent.value, thread_id)
        self._loaded[agent] = thread_id
        return thread_id

    def forget(self, agent: AgentType) -> None:
        self._loaded.pop(agent, None)
