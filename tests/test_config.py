import os
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from config.settings import Settings, load_settings


def test_configuration_requires_explicit_paper_environment() -> None:
    with pytest.raises(RuntimeError, match="TRADING_ENV"):
        load_settings(environ={})
    settings = load_settings(environ={"TRADING_ENV": "paper"})
    assert settings.broker.paper is True


def test_configuration_loads_project_environment_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TRADING_ENV=paper\nCODEX_EXECUTABLE=C:/tools/codex.exe\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TRADING_ENV", raising=False)
    monkeypatch.delenv("CODEX_EXECUTABLE", raising=False)

    settings = load_settings(env_path=env_file)

    assert settings.broker.paper is True
    assert os.environ["TRADING_ENV"] == "paper"
    assert os.environ["CODEX_EXECUTABLE"] == "C:/tools/codex.exe"


def test_shell_environment_takes_precedence_over_environment_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TRADING_ENV=live\n", encoding="utf-8")
    monkeypatch.setenv("TRADING_ENV", "paper")

    settings = load_settings(env_path=env_file)

    assert settings.broker.paper is True
    assert os.environ["TRADING_ENV"] == "paper"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("broker", "paper"), False),
        (("app", "environment"), "live"),
        (("execution", "allow_live_trading"), True),
        (("execution", "allow_margin"), True),
        (("execution", "allow_shorting"), True),
        (("execution", "kill_switch"), {"enabled": False}),
    ],
)
def test_configuration_rejects_unsafe_modes(path: tuple[str, str], value: object) -> None:
    with open("config/settings.yaml", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    unsafe = deepcopy(raw)
    unsafe[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        Settings.model_validate(unsafe)
