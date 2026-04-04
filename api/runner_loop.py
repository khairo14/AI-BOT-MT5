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
    "scalping":    5,    # M1 bars close every 60 s — 5 s keeps latency within 1 bar
    "day_trading": 60,
    "swing":       300,
}

_runner_task: Optional[asyncio.Task] = None
_mode_tasks: dict[str, asyncio.Task] = {}  # NEW-7: per-mode task refs to detect accumulation
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


async def _run_one_mode(runner, bus, mode: str, sym_override) -> None:
    """Run a single mode scan as a fire-and-forget task — non-blocking."""
    try:
        signals = await asyncio.to_thread(runner.run_mode, mode, sym_override)
        for sig in signals:
            await bus.add_signal(_signal_to_dict(sig, mode))
            logger.debug(f"Runner signal queued: {mode}/{sig.symbol}/{sig.strategy}")
    except Exception as exc:
        logger.exception(f"Runner loop error [{mode}]: {exc}")


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

        # Auto-reconnect: if MT5 dropped, attempt reconnection before proceeding.
        # This handles terminal restarts, network hiccups, and broker disconnections.
        if not client.is_connected():
            logger.warning("Strategy runner: MT5 disconnected — attempting reconnect...")
            try:
                reconnected = await asyncio.to_thread(client.reconnect)
                if not reconnected:
                    logger.error("Strategy runner: MT5 reconnect failed — skipping tick")
                    await asyncio.sleep(30)
                    continue
                logger.info("Strategy runner: MT5 reconnected successfully")
            except Exception as _rc_exc:
                logger.error(f"Strategy runner: MT5 reconnect error: {_rc_exc}")
                await asyncio.sleep(30)
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
                
        # Weekend gap protection — close swing positions before Friday market close
        # Forex closes ~22:00 UTC Friday; indices/stocks close ~21:00 UTC Friday.
        # Check every 5 minutes (every 60 ticks at 5s base) to avoid hammering MT5.
        try:
            _now_utc = datetime.now(tz=timezone.utc)
            _is_friday = _now_utc.weekday() == 4  # Friday = 4
            _hour = _now_utc.hour
            _minute = _now_utc.minute
            # Trigger between 20:45–21:00 UTC on Fridays
            if _is_friday and _hour == 20 and 45 <= _minute < 60:
                from engine.account_store import current_mode as _cm
                _cfg_path = CONFIG_DIR / "app.json"
                try:
                    _app_cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                    _gap_protect = _app_cfg.get("weekend_gap_protection", True)
                except Exception:
                    _gap_protect = True
                if _gap_protect:
                    _positions = await asyncio.to_thread(client.get_open_positions)
                    _swing_open = [
                        p for p in _positions
                        if str(p.get("comment", "")).startswith("swing")
                    ]
                    if _swing_open:
                        logger.warning(
                            f"Weekend gap protection: closing {len(_swing_open)} "
                            f"swing position(s) before Friday close"
                        )
                        from engine.order_manager import OrderManager
                        _om = OrderManager(client)
                        for _pos in _swing_open:
                            try:
                                await asyncio.to_thread(
                                    _om.close_position,
                                    _pos["ticket"],
                                    "weekend_gap_protection"
                                )
                                logger.info(
                                    f"Weekend gap: closed #{_pos['ticket']} "
                                    f"{_pos['symbol']} swing"
                                )
                            except Exception as _wge:
                                logger.warning(
                                    f"Weekend gap: failed to close #{_pos['ticket']}: {_wge}"
                                )
        except Exception as _wg_exc:
            logger.debug(f"Weekend gap check error: {_wg_exc}")

        for mode, interval in INTERVALS.items():
            counters[mode] += 5
            if counters[mode] >= interval:
                counters[mode] = 0
                # Re-read scanner.json every tick so dashboard changes apply immediately
                _sym_override = None
                try:
                    _scan_raw = json.loads((CONFIG_DIR / "scanner.json").read_text(encoding="utf-8-sig"))
                    if not isinstance(_scan_raw, dict):
                        raise ValueError("scanner.json root must be a JSON object")
                    _scan = _scan_raw.get(mode, {})
                    if not isinstance(_scan, dict):
                        raise ValueError(f"scanner.json[{mode!r}] must be an object")
                    if not _scan.get("enabled", False):
                        logger.debug(f"Scanner [{mode}] is paused — skipping")
                        continue  # scanner disabled for this mode
                    _sym_override = [s for s in _scan.get("symbols", []) if s] or None
                except FileNotFoundError:
                    pass  # scanner.json missing — scan all enabled symbols
                except (json.JSONDecodeError, ValueError) as _err:
                    logger.warning(f"scanner.json corrupt/invalid [{mode}]: {_err} — scanning all symbols")
                except Exception as _err:
                    logger.debug(f"Scanner config read error [{mode}]: {_err}")
                # NEW-7: skip if previous task for this mode is still running —
                # prevents task accumulation under high MT5 latency.
                _prev = _mode_tasks.get(mode)
                if _prev and not _prev.done():
                    logger.debug(f"Runner [{mode}]: previous task still running — skipping tick")
                    continue
                _t = asyncio.create_task(_run_one_mode(runner, bus, mode, _sym_override))
                _mode_tasks[mode] = _t


def _signal_to_dict(sig, mode: str) -> dict:
    # Compute R:R if entry, sl, tp are available
    rr = None
    try:
        if sig.entry_price and sig.sl_price and sig.tp_price:
            risk = abs(sig.entry_price - sig.sl_price)
            reward = abs(sig.tp_price - sig.entry_price)
            if risk > 0:
                rr = round(reward / risk, 2)
    except Exception:
        pass
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
        "timeframe":    sig.timeframe,
        "note":         sig.comment,
        "rr":           rr,
        "tp2":          sig.tp2_price if hasattr(sig, "tp2_price") else None,
        "indicators":   sig.indicators if hasattr(sig, "indicators") else {},
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
