from __future__ import annotations

import asyncio
import inspect
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from core.models import AgentType, OrderStatus, TradeAction, TradeUpdate

TradeUpdateHandler = Callable[[TradeUpdate], Awaitable[None]]
StateHandler = Callable[[bool, str], Awaitable[None]]


def normalize_trade_update(
    agent: AgentType,
    raw: Any,
    previous_filled: Decimal = Decimal("0"),
) -> TradeUpdate:
    raw_event = _get(raw, "event", "submitted")
    event = str(getattr(raw_event, "value", raw_event)).lower()
    order = _get(raw, "order", raw)
    cumulative = Decimal(str(_get(order, "filled_qty", 0) or 0))
    event_quantity = _get(raw, "qty", None)
    delta = (
        Decimal(str(event_quantity))
        if event_quantity is not None
        else max(Decimal("0"), cumulative - previous_filled)
    )
    price_value = _get(raw, "price", None) or _get(order, "filled_avg_price", None)
    raw_side = _get(order, "side", "")
    side = str(getattr(raw_side, "value", raw_side)).lower()
    action = TradeAction.BUY if side == "buy" else TradeAction.SELL if side == "sell" else None
    statuses = {
        "submitted": OrderStatus.SUBMITTED,
        "pending_new": OrderStatus.SUBMITTED,
        "new": OrderStatus.ACCEPTED,
        "accepted": OrderStatus.ACCEPTED,
        "partial_fill": OrderStatus.PARTIALLY_FILLED,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "fill": OrderStatus.FILLED,
        "filled": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELLED,
        "cancelled": OrderStatus.CANCELLED,
        "rejected": OrderStatus.REJECTED,
    }
    raw_timestamp = _get(raw, "timestamp", None) or _get(order, "updated_at", None)
    timestamp = datetime.now(UTC) if raw_timestamp is None else _timestamp(raw_timestamp)
    return TradeUpdate(
        agent=agent,
        order_id=str(_get(order, "id", "unknown")),
        client_order_id=str(_get(order, "client_order_id", "unknown")),
        symbol=_normalize_symbol(str(_get(order, "symbol", "unknown"))),
        action=action,
        status=statuses.get(event, OrderStatus.SUBMITTED),
        cumulative_filled_quantity=cumulative,
        fill_delta_quantity=delta,
        fill_price=None if price_value is None else Decimal(str(price_value)),
        timestamp=timestamp,
    )


class AlpacaOrderStream:
    """Bounded-retry wrapper around Alpaca's blocking trade-update stream."""

    def __init__(
        self,
        agent: AgentType,
        connection_factory: Callable[[], Any],
        update_handler: TradeUpdateHandler,
        state_handler: StateHandler,
        *,
        max_reconnect_attempts: int = 5,
        reconnect_base_seconds: float = 1,
        reconnect_max_seconds: float = 30,
        startup_timeout_seconds: float = 15,
        shutdown_timeout_seconds: float = 5,
    ) -> None:
        self.agent = agent
        self._factory = connection_factory
        self._update_handler = update_handler
        self._state_handler = state_handler
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_base_seconds = reconnect_base_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._connection: Any = None
        self._runner: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._filled: dict[str, Decimal] = {}

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and not self._stop.is_set()

    def start(self) -> asyncio.Task[None]:
        if self._runner is None or self._runner.done():
            self._owner_loop = asyncio.get_running_loop()
            self._stop.clear()
            self._runner = asyncio.create_task(self.run_forever())
        return self._runner

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=self.startup_timeout_seconds)

    async def run_forever(self) -> None:
        if self._owner_loop is None:
            self._owner_loop = asyncio.get_running_loop()
        for attempt in range(1, self.max_reconnect_attempts + 1):
            if self._stop.is_set():
                return
            self._ready.clear()
            try:
                connection = self._factory()
                self._connection = connection
                connection.subscribe_trade_updates(self._on_raw_update)
                task = asyncio.create_task(_run_connection(connection))
                await self._wait_connected(connection, task)
                await self._state_handler(True, "trade update stream connected")
                self._ready.set()
                await task
                if self._stop.is_set():
                    return
                raise ConnectionError("trade update stream stopped unexpectedly")
            except Exception as exc:
                self._ready.clear()
                await self._state_handler(
                    False,
                    f"trade update stream failure ({attempt}/{self.max_reconnect_attempts}): "
                    f"{type(exc).__name__}",
                )
                await self._close_connection()
                if attempt >= self.max_reconnect_attempts:
                    return
                delay = min(
                    self.reconnect_max_seconds,
                    self.reconnect_base_seconds * (2 ** (attempt - 1)),
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    continue

    async def stop(self) -> None:
        self._stop.set()
        self._ready.clear()
        await self._close_connection()
        if self._runner is not None:
            try:
                await asyncio.wait_for(
                    self._runner,
                    timeout=self.shutdown_timeout_seconds,
                )
            except TimeoutError:
                self._runner.cancel()
                await asyncio.gather(self._runner, return_exceptions=True)

    async def _wait_connected(self, connection: Any, task: asyncio.Task[None]) -> None:
        deadline = asyncio.get_running_loop().time() + self.startup_timeout_seconds
        while not _connection_ready(connection):
            if task.done():
                await task
                raise ConnectionError("trade stream stopped before connecting")
            if asyncio.get_running_loop().time() >= deadline:
                task.cancel()
                raise TimeoutError("trade stream connection timed out")
            await asyncio.sleep(0.01)

    async def _on_raw_update(self, raw: Any) -> None:
        order = _get(raw, "order", raw)
        order_id = str(_get(order, "id", "unknown"))
        update = normalize_trade_update(
            self.agent,
            raw,
            self._filled.get(order_id, Decimal("0")),
        )
        self._filled[order_id] = update.cumulative_filled_quantity
        owner_loop = self._owner_loop
        current_loop = asyncio.get_running_loop()
        if owner_loop is None or owner_loop is current_loop:
            await self._update_handler(update)
            return
        future = asyncio.run_coroutine_threadsafe(self._update_handler(update), owner_loop)
        await asyncio.wrap_future(future)

    async def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        stop = getattr(connection, "stop", None)
        if stop is not None:
            try:
                if inspect.iscoroutinefunction(stop):
                    await asyncio.wait_for(stop(), timeout=self.shutdown_timeout_seconds)
                else:
                    result = await asyncio.wait_for(
                        _run_in_daemon_thread(stop),
                        timeout=self.shutdown_timeout_seconds,
                    )
                    if inspect.isawaitable(result):
                        await asyncio.wait_for(result, timeout=self.shutdown_timeout_seconds)
            except TimeoutError:
                # The stream is already execution-blocking at this point. Never let
                # an alpaca-py stop() deadlock prevent process shutdown and restart.
                return


def build_alpaca_order_stream(
    agent: AgentType,
    update_handler: TradeUpdateHandler,
    state_handler: StateHandler,
    **settings: Any,
) -> AlpacaOrderStream:
    if os.environ.get("TRADING_ENV") != "paper":
        raise RuntimeError("TRADING_ENV must be paper before creating an Alpaca stream")
    prefix = "ALPACA_AGGRESSIVE" if agent is AgentType.AGGRESSIVE else "ALPACA_CONSERVATIVE"
    key = os.environ.get(f"{prefix}_API_KEY")
    secret = os.environ.get(f"{prefix}_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError(f"missing Alpaca paper credentials for {agent.value}")

    def factory() -> Any:
        from alpaca.trading.stream import TradingStream

        class SingleConnectionTradingStream(TradingStream):
            async def _wait_before_reconnect(self, retries: int) -> None:
                raise ConnectionError(f"Alpaca stream disconnected after {retries} attempts")

        return SingleConnectionTradingStream(key, secret, paper=True)

    return AlpacaOrderStream(agent, factory, update_handler, state_handler, **settings)


async def _run_connection(connection: Any) -> None:
    run = connection.run
    if inspect.iscoroutinefunction(run):
        await run()
    else:
        await _run_in_daemon_thread(run)


async def _run_in_daemon_thread(function: Callable[[], Any]) -> Any:
    """Run blocking SDK code without making interpreter shutdown depend on it."""

    loop = asyncio.get_running_loop()
    completed: asyncio.Future[Any] = loop.create_future()

    def finish_result(result: Any) -> None:
        if not completed.done():
            completed.set_result(result)

    def finish_error(error: BaseException) -> None:
        if not completed.done():
            completed.set_exception(error)

    def target() -> None:
        try:
            result = function()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(finish_error, exc)
            except RuntimeError:
                return
        else:
            try:
                loop.call_soon_threadsafe(finish_result, result)
            except RuntimeError:
                return

    threading.Thread(target=target, daemon=True).start()
    return await completed


def _connection_ready(connection: Any) -> bool:
    check = getattr(connection, "is_connected", None)
    if callable(check):
        return bool(check())
    if hasattr(connection, "connected"):
        return bool(connection.connected)
    return bool(getattr(connection, "_running", False))


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _timestamp(value: Any) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Alpaca stream returned a naive timestamp")
    return parsed.astimezone(UTC)


def _normalize_symbol(symbol: str) -> str:
    return {
        "BTCUSD": "BTC/USD",
        "ETHUSD": "ETH/USD",
        "SOLUSD": "SOL/USD",
    }.get(symbol, symbol)
