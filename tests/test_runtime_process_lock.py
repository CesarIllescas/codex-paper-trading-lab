from pathlib import Path

import pytest

from runtime.process_lock import RuntimeAlreadyRunningError, RuntimeProcessLock


def test_runtime_process_lock_rejects_second_runtime_and_is_recoverable(tmp_path: Path) -> None:
    path = tmp_path / "trading.lock"
    with RuntimeProcessLock(path):
        with pytest.raises(RuntimeAlreadyRunningError):
            with RuntimeProcessLock(path):
                pass
    with RuntimeProcessLock(path):
        assert path.exists()
    assert path.read_text(encoding="ascii")
