from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from core.models import AgentType, CodexDecision, MarketSnapshot, PortfolioSnapshot, Signal
from intelligence.codex_app_server import CodexAppServer
from intelligence.codex_threads import CodexThreadManager
from intelligence.schemas import CODEX_DECISION_SCHEMA, CodexOutcome


class CodexClient:
    def __init__(
        self,
        agent: AgentType,
        server: CodexAppServer,
        threads: CodexThreadManager,
        prompt_file: Path,
    ) -> None:
        if agent not in {AgentType.AGGRESSIVE, AgentType.CONSERVATIVE}:
            raise ValueError("Codex clients exist only for Codex agent portfolios")
        self.agent = agent
        self.server = server
        self.threads = threads
        self.system_prompt = prompt_file.read_text(encoding="utf-8")

    async def decide(
        self,
        market: MarketSnapshot,
        signals: list[Signal],
        portfolio: PortfolioSnapshot,
        recent_decisions: list[dict[str, object]] | None = None,
    ) -> CodexOutcome:
        context = {
            "decision_timestamp": market.decision_timestamp.isoformat(),
            "symbol": market.symbol,
            "market_price": str(market.price),
            "market_snapshot_id": market.snapshot_id,
            "latest_closed_bar_at": market.latest_bar_at.isoformat(),
            "indicators": market.indicators,
            "strategy_signals": [item.model_dump(mode="json") for item in signals],
            "portfolio": {
                "positions": [item.model_dump(mode="json") for item in portfolio.positions],
                "cash": str(portfolio.cash),
                "equity": str(portfolio.equity),
                "current_exposure": str(portfolio.exposure),
                "realized_pnl": str(portfolio.realized_pnl),
                "unrealized_pnl": str(portfolio.unrealized_pnl),
                "drawdown_pct": str(portfolio.drawdown_pct),
            },
            "recent_decisions": recent_decisions or [],
            "quantity_instruction": "Do not calculate or recommend order quantity or notional.",
        }
        text = f"{self.system_prompt}\n\nCURRENT AUTHORITATIVE INPUT:\n{json.dumps(context)}"
        try:
            thread_id = await self.threads.get_or_create(self.agent, self.server)
            response = await self.server.run_turn(thread_id, text, CODEX_DECISION_SCHEMA)
            decision = CodexDecision.model_validate_json(response)
            if decision.symbol != market.symbol:
                raise ValueError("Codex returned a different symbol")
            return CodexOutcome(decision=decision, valid=True)
        except (Exception, ValidationError) as exc:
            await self.server.stop()
            self.threads.forget(self.agent)
            return CodexOutcome(decision=None, valid=False, error=type(exc).__name__)
