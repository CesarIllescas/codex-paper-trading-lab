from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from config.settings import RiskLimits
from core.models import MarketSnapshot, PortfolioSnapshot, TradeIntent


@dataclass(slots=True)
class KillSwitchState:
    enabled: bool = True
    duplicate_order: bool = False
    api_error_count: int = 0
    max_api_errors: int = 3
    portfolio_state_valid: bool = True
    broker_state_valid: bool = True
    extra_reasons: list[str] = field(default_factory=list)


def kill_switch_reasons(
    intent: TradeIntent,
    portfolio: PortfolioSnapshot,
    market: MarketSnapshot,
    limits: RiskLimits,
    state: KillSwitchState,
) -> list[str]:
    if not state.enabled:
        return []
    reasons = list(state.extra_reasons)
    if market.stale:
        reasons.append("stale market data")
    if market.price <= 0:
        reasons.append("invalid market price")
    if not portfolio.reconciled or not state.portfolio_state_valid:
        reasons.append("portfolio state cannot be reconciled")
    if not state.broker_state_valid:
        reasons.append("broker state cannot be reconciled")
    if state.duplicate_order:
        reasons.append("duplicate order detected")
    if state.api_error_count >= state.max_api_errors:
        reasons.append("excessive API errors")
    if not intent.codex_valid:
        reasons.append("Codex response cannot be validated")
    equity = portfolio.equity
    if portfolio.daily_pnl <= -(equity * Decimal(str(limits.max_daily_loss_pct))):
        reasons.append("daily loss limit exceeded")
    if portfolio.drawdown_pct >= Decimal(str(limits.max_drawdown_pct)):
        reasons.append("maximum drawdown exceeded")
    return list(dict.fromkeys(reasons))
