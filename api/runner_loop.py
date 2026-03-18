"""
Strategy runner background loop.

Runs all 9 strategies on a per-mode schedule inside the FastAPI process
and feeds new signals into the SignalBus.

The MT5 API is blocking/synchronous, so all strategy work runs via
asyncio.to_thread() to avoid blocking the event loop.

Intervals (seconds, start running immediately on first tick):
  scalping:    10 s
  day_trading: 60 s
  swing:       300 s (5 min)
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

INTERVALS: dict[str, int] = {
    "scalping":    10,
    "day_trading": 60,
    "swing":       300,
}

_runner_task: Optional[asyncio.Task] = None


async def _runner_loop(client, order_manager, risk_manager) -> None:
    from api.signal_bus import bus
    from engine.strategy_runner import StrategyRunner

    runner = StrategyRunner(
        client=client,
        order_manager=order_manager,
        risk_manager=risk_manager,
        execution_mode="manual",  # SignalBus handles auto vs manual
    )

    # Start counters at their interval so each mode runs on the first tick
    counters = dict(INTERVALS)
    logger.info(f"Strategy runner loop started. Intervals: {INTERVALS}")

    while True:
        await asyncio.sleep(5)  # base tick

        for mode, interval in INTERVALS.items():
            counters[mode] += 5
            if counters[mode] >= interval:
                counters[mode] = 0
                try:
                    signals = await asyncio.to_thread(runner.run_mode, mode)
                    for sig in signals:
                        await bus.add_signal(_signal_to_dict(sig, mode))
                        logger.debug(
                            f"Runner signal queued: {mode}/{sig.symbol}/{sig.strategy}"
                        )
                except Exception as exc:
                    logger.exception(f"Runner loop error [{mode}]: {exc}")


def _signal_to_dict(sig, mode: str) -> dict:
    return {
        "id":           str(uuid.uuid4()),
        "status":       "pending",
        "created_at":   datetime.now(tz=ZoneInfo("UTC")).isoformat(),
        "symbol":       sig.symbol,
        "direction":    sig.direction,
        "trading_mode": mode,
        "strategy":     sig.strategy,
        "entry_price":  sig.entry_price,
        "sl":           sig.sl_price,
        "tp":           sig.tp_price,
        "lot_size":     sig.lot_size,
        "confidence":   None,
        "timeframe":    "",
        "note":         sig.comment,
    }


def start_runner_loop(client, order_manager, risk_manager) -> None:
    """Start the background strategy runner (idempotent)."""
    global _runner_task
    if _runner_task is None or _runner_task.done():
        _runner_task = asyncio.create_task(
            _runner_loop(client, order_manager, risk_manager)
        )
        logger.info("Strategy runner task created.")
