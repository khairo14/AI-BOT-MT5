"""
Strategy runner background loop.

Runs all 9 strategies on a per-mode schedule inside the FastAPI process
and feeds new signals into the SignalBus.

The MT5 API is blocking/synchronous, so all strategy work runs via
asyncio.to_thread() to avoid blocking the event loop.

Intervals (seconds, start running immediately on first tick):
  scalping:    30 s  (M5 bars close every 300 s — 30 s is plenty reactive)
  day_trading: 60 s
  swing:       300 s (5 min)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

CONFIG_DIR = Path(__file__).parent.parent / "config"

INTERVALS: dict[str, int] = {
    "scalping":    30,
    "day_trading": 60,
    "swing":       300,
}

_runner_task: Optional[asyncio.Task] = None
_risk_manager = None  # exposed so /risk/status can read live state
_paused: bool = False  # set True during account mode switch


def pause_runner() -> None:
    """Signal the loop to skip iterations (used during account switching)."""
    global _paused
    _paused = True
    logger.info("Strategy runner paused.")


def resume_runner() -> None:
    """Resume the runner after account switching."""
    global _paused
    _paused = False
    logger.info("Strategy runner resumed.")


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
    _paper_sync_counter = 0
    logger.info(f"Strategy runner loop started. Intervals: {INTERVALS}")

    while True:
        await asyncio.sleep(5)  # base tick

        if _paused:
            continue

        # Sync paper trade ledger every 60 s when in paper mode
        _paper_sync_counter += 5
        if _paper_sync_counter >= 60:
            _paper_sync_counter = 0
            try:
                from engine.account_store import current_mode
                if current_mode() == "paper":
                    from engine.paper_trade import paper_engine
                    if paper_engine is not None:
                        await asyncio.to_thread(paper_engine.sync_positions)
            except Exception as _exc:
                logger.debug(f"paper sync error: {_exc}")

        for mode, interval in INTERVALS.items():
            counters[mode] += 5
            if counters[mode] >= interval:
                counters[mode] = 0
                # Re-read scanner.json every tick so dashboard changes apply immediately
                _sym_override = None
                try:
                    _scan = json.loads((CONFIG_DIR / "scanner.json").read_text()).get(mode, {})
                    if not _scan.get("enabled", False):
                        logger.debug(f"Scanner [{mode}] is paused — skipping")
                        continue  # scanner disabled for this mode
                    _sym_override = [s for s in _scan.get("symbols", []) if s] or None
                except FileNotFoundError:
                    pass  # scanner.json missing — scan all enabled symbols
                except Exception as _err:
                    logger.debug(f"Scanner config read error [{mode}]: {_err}")
                try:
                    signals = await asyncio.to_thread(runner.run_mode, mode, _sym_override)
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
        "created_at":   datetime.now(tz=timezone.utc).isoformat(),
        "symbol":       sig.symbol,
        "direction":    sig.direction,
        "trading_mode": mode,
        "strategy":     sig.strategy,
        "entry_price":  sig.entry_price,
        "sl":           sig.sl_price,
        "tp":           sig.tp_price,
        "lot_size":     sig.lot_size,
        "confidence":   sig.confidence if sig.confidence > 0 else None,
        "timeframe":    "",
        "note":         sig.comment,
    }


def start_runner_loop(client, order_manager, risk_manager) -> None:
    """Start the background strategy runner (idempotent)."""
    global _runner_task, _risk_manager
    _risk_manager = risk_manager
    if _runner_task is None or _runner_task.done():
        _runner_task = asyncio.create_task(
            _runner_loop(client, order_manager, risk_manager)
        )
        logger.info("Strategy runner task created.")
