from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, model_validator

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StrictSettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSettings(StrictSettingsModel):
    environment: str
    timezone: str = "UTC"
    log_level: str = "INFO"


class BrokerSettings(StrictSettingsModel):
    provider: str
    paper: bool


class CodexRuntimeSettings(StrictSettingsModel):
    executable: str = "codex"
    mode: str = "app-server"
    workspace_root: Path
    startup_timeout_seconds: float = Field(gt=0)
    turn_timeout_seconds: float = Field(gt=0)


class CodexThreadSettings(StrictSettingsModel):
    persistent: bool
    prompt_file: Path


class CodexResponseSettings(StrictSettingsModel):
    require_structured_output: bool = True
    schema_validation: bool = True
    failure_action: str = "HOLD"


class CodexSettings(StrictSettingsModel):
    enabled: bool = True
    runtime: CodexRuntimeSettings
    threads: dict[str, CodexThreadSettings]
    response: CodexResponseSettings


class UniverseSettings(StrictSettingsModel):
    timeframe: str
    universe: list[str]


class MarketSettings(StrictSettingsModel):
    stale_data_seconds: int = Field(gt=0)
    aggressive: UniverseSettings
    conservative: UniverseSettings


class PortfolioSettings(StrictSettingsModel):
    initial_budget: float = Field(gt=0)


class StrategyToggle(StrictSettingsModel):
    enabled: bool
    weight: float = Field(ge=0, le=1)


class RiskLimits(StrictSettingsModel):
    risk_per_trade: float = Field(gt=0, le=1)
    max_position_pct: float = Field(gt=0, le=1)
    max_total_exposure_pct: float = Field(gt=0, le=1)
    max_daily_loss_pct: float = Field(gt=0, le=1)
    max_drawdown_pct: float = Field(gt=0, le=1)
    max_open_positions: int = Field(gt=0)


class DecisionSettings(StrictSettingsModel):
    minimum_codex_confidence: float = Field(ge=0, le=1)


class AgentSettings(StrictSettingsModel):
    enabled: bool
    portfolio: PortfolioSettings
    strategies: dict[str, StrategyToggle]
    risk: RiskLimits
    decision: DecisionSettings


class BaselineSettings(StrictSettingsModel):
    enabled: bool
    initial_budget: float = Field(gt=0)
    symbol: str | None = None


class KillSwitchSettings(StrictSettingsModel):
    enabled: bool = True


class ExecutionSettings(StrictSettingsModel):
    dry_run: bool = True
    reconcile_orders: bool = True
    duplicate_order_window_seconds: int = Field(gt=0)
    kill_switch: KillSwitchSettings
    allow_live_trading: bool = False
    allow_margin: bool = False
    allow_shorting: bool = False
    allow_options: bool = False
    allow_futures: bool = False


class StorageSettings(StrictSettingsModel):
    type: str
    database_url: str


class LogTargetSettings(StrictSettingsModel):
    enabled: bool
    path: Path
    format: str | None = None


class LoggingSettings(StrictSettingsModel):
    decisions: LogTargetSettings
    codex: LogTargetSettings


class SchedulerSettings(StrictSettingsModel):
    candle_grace_seconds: int = Field(default=5, ge=0)
    reconciliation_interval_seconds: int = Field(default=300, gt=0)
    health_interval_seconds: int = Field(default=60, gt=0)


class StreamSettings(StrictSettingsModel):
    max_reconnect_attempts: int = Field(default=5, ge=1)
    reconnect_base_seconds: float = Field(default=1.0, gt=0)
    reconnect_max_seconds: float = Field(default=30.0, gt=0)
    startup_timeout_seconds: float = Field(default=15.0, gt=0)


class AutonomousRuntimeSettings(StrictSettingsModel):
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    stream: StreamSettings = Field(default_factory=StreamSettings)


class BacktestSettings(StrictSettingsModel):
    initial_capital: float = Field(default=10000, gt=0)
    slippage_bps: float = Field(default=2.0, ge=0)
    transaction_cost_bps: float = Field(default=1.0, ge=0)
    fill_fraction: float = Field(default=1.0, gt=0, le=1)
    warmup_bars: int = Field(default=50, ge=1)
    codex_signal_threshold: float = Field(default=0.65, ge=0, le=1)


class Settings(StrictSettingsModel):
    app: AppSettings
    broker: BrokerSettings
    codex: CodexSettings
    market: MarketSettings
    agents: dict[str, AgentSettings]
    baselines: dict[str, BaselineSettings]
    execution: ExecutionSettings
    storage: StorageSettings
    logging: LoggingSettings
    runtime: AutonomousRuntimeSettings = Field(default_factory=AutonomousRuntimeSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)

    @model_validator(mode="after")
    def enforce_paper_only(self) -> Settings:
        if self.app.environment != "paper":
            raise ValueError("app.environment must be 'paper'")
        if self.broker.provider != "alpaca" or self.broker.paper is not True:
            raise ValueError("Alpaca broker must explicitly use paper=true")
        if self.codex.runtime.mode != "app-server":
            raise ValueError("Codex must use persistent app-server mode")
        if not self.execution.kill_switch.enabled:
            raise ValueError("the deterministic kill switch cannot be disabled")
        prohibited = {
            "live trading": self.execution.allow_live_trading,
            "margin": self.execution.allow_margin,
            "shorting": self.execution.allow_shorting,
            "options": self.execution.allow_options,
            "futures": self.execution.allow_futures,
        }
        enabled = [name for name, value in prohibited.items() if value]
        if enabled:
            raise ValueError(f"prohibited execution features enabled: {', '.join(enabled)}")
        return self


def load_settings(
    path: str | Path = "config/settings.yaml",
    environ: Mapping[str, str] | None = None,
    *,
    env_path: str | Path | None = None,
) -> Settings:
    if environ is None:
        load_dotenv(
            dotenv_path=Path(env_path) if env_path is not None else _PROJECT_ROOT / ".env",
            override=False,
        )
    environment = os.environ if environ is None else environ
    if environment.get("TRADING_ENV") != "paper":
        raise RuntimeError("TRADING_ENV must be explicitly set to 'paper'")
    with Path(path).open(encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle)
    return Settings.model_validate(raw)
