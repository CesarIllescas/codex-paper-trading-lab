from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class RuntimeAlreadyRunningError(RuntimeError):
    pass


class RuntimeProcessLock:
    """Hold a recoverable OS file lock for one trading runtime per workspace."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._handle: BinaryIO | None = None

    def __enter__(self) -> RuntimeProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            _lock(handle)
        except OSError as exc:
            handle.close()
            raise RuntimeAlreadyRunningError(
                "another trading runtime is already running in this workspace"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        handle.seek(0)
        self._handle = handle
        return self

    def __exit__(self, *_: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            _unlock(handle)
        finally:
            handle.close()


if os.name == "nt":
    import msvcrt

    def _lock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: BinaryIO) -> None:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: BinaryIO) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
