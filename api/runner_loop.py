"""
Strategy runner background loop.

Runs all 9 strategies on a per-mode schedule inside the FastAPI process
and feeds new signals into the SignalBus.

The MT5 API is blocking/synchronous, so all strategy work runs via
asyncio.to_thread() to avoid blocking the event loop.

Bar-close guard: each mode only runs when a new bar has closed since the
last scan. This prevents redundant OHLCV fetches on every 5-second base
tick, reducing MT5 lock contention by ~90% for scalping (M1 bars close
every 60 s, not every 5 s).

Intervals (seconds between checks — actual execution gated by bar close):
  scalping:    5 s check, fires on new M1 bar  (~60 s between runs)
  day_trading: 60 s check, fires on new H1 bar (~3600 s between runs)
  swing:       300 s check, fires on new H4 bar (~14400 s between runs)
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
    "scalping":    5,    # check every 5 s; bar-close guard fires only on new M1 bar
    "day_trading": 60,
    "swing":       300,
}

# Primary timeframe per mode — used by bar-close guard to detect new bar
_MODE_PRIMARY_TF: dict[str, str] = {
    "scalping":    "M1",
    "day_trading": "H1",
    "swing":       "H4",
}

# Bar-close guard: track the timestamp of the last bar seen per mode.
# A mode scan only runs when the latest bar timestamp advances beyond this value.
# Keyed by mode string. Values are pd.Timestamp or None on first run.
_last_bar_time: dict[str, object] = {
    "scalping": None,
    "day_trading": None,
    "swing": None,
}

_runner_task: Optional[asyncio.Task] = None
_news_refresh_task: Optional[asyncio.Task] = None
_trailing_stop_task: Optional[asyncio.Task] = None
_mode_tasks: dict[str, asyncio.Task] = {}  # per-mode task refs to detect accumulation
_risk_manager = None  # exposed so /risk/status can read live state
_trailing_stop_manager = None  # exposed so /positions endpoint can read trailing status
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


def _get_probe_symbol(mode: str) -> str:
    """Return the first enabled symbol for a mode from symbols.json, default EURUSD."""
    try:
        syms_cfg = json.loads(
            (CONFIG_DIR / "symbols.json").read_text(encoding="utf-8-sig")
        )
        for entry in syms_cfg.get(mode, []):
            if isinstance(entry, dict) and entry.get("enabled", False):
                return entry["symbol"]
    except Exception:
        pass
    return "EURUSD"


def _new_bar_closed(client, mode: str) -> bool:
    """
    Return True if a new primary-timeframe bar has closed since the last scan.
    Fetches only the latest 2 bars (minimal MT5 lock time) to compare timestamps.

    First call always returns True so each mode runs immediately at startup.
    Falls back to True on any MT5 error so a data failure never silently
    suppresses trading — the strategy runner handles missing data gracefully.
    """
    global _last_bar_time
    tf     = _MODE_PRIMARY_TF.get(mode, "M1")
    symbol = _get_probe_symbol(mode)
    try:
        df = client.get_ohlcv(symbol, tf, count=2)
        if df is None or df.empty:
            return True   # fail-open
        latest = df.iloc[-1]["time"]
        prev   = _last_bar_time.get(mode)
        if prev is None or latest > prev:
            _last_bar_time[mode] = latest
            return True
        return False
    except Exception:
        return True   # fail-open: never suppress trading on a fetch error


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
    _weekend_check_counter = 0  # Check weekend gap protection every 5 minutes
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
        # Check every 5 minutes (60 ticks at 5s base) during Friday 18:00-22:00 UTC
        # and Saturday 00:00-02:00 UTC as a fallback for any remaining positions.
        _weekend_check_counter += 1
        if _weekend_check_counter >= 60:  # Every 5 minutes
            _weekend_check_counter = 0
            try:
                _now_utc = datetime.now(tz=timezone.utc)
                _is_friday = _now_utc.weekday() == 4  # Friday = 4
                _is_saturday = _now_utc.weekday() == 5  # Saturday = 5
                _hour = _now_utc.hour
                # Trigger on Friday 18:00-22:00 UTC or Saturday 00:00-02:00 UTC (fallback)
                if (_is_friday and 18 <= _hour < 22) or (_is_saturday and 0 <= _hour < 2):
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
                                f"Weekend gap protection [{_now_utc.strftime('%a %H:%M UTC')}]: "
                                f"closing {len(_swing_open)} swing position(s) before weekend"
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
                                        f"{_pos['symbol']} swing (P&L: ${_pos.get('profit', 0):.2f})"
                                    )
                                except Exception as _wge:
                                    logger.warning(
                                        f"Weekend gap: failed to close #{_pos['ticket']}: {_wge}"
                                    )
                        # Log check even if no positions found (helps with debugging)
                        elif _is_friday and 20 <= _hour < 21:
                            logger.debug(
                                f"Weekend gap check [{_now_utc.strftime('%H:%M UTC')}]: "
                                f"no swing positions to close"
                            )
            except Exception as _wg_exc:
                logger.debug(f"Weekend gap check error: {_wg_exc}")

        for mode, interval in INTERVALS.items():
            counters[mode] += 5
            if counters[mode] >= interval:
                counters[mode] = 0
                # Bar-close guard: only run strategies when a new primary-TF bar
                # has closed since the last scan. This avoids redundant OHLCV fetches
                # (and MT5 lock contention) on every 5 s base tick for scalping.
                # The guard uses a lightweight 2-bar fetch on the probe symbol.
                # Falls back to True on any error so a data issue never blocks trading.
                if not await asyncio.to_thread(_new_bar_closed, client, mode):
                    logger.debug(f"Runner [{mode}]: no new bar — skipping tick")
                    continue
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
                # Skip if previous task for this mode is still running —
                # prevents task accumulation under high MT5 latency.
                _prev = _mode_tasks.get(mode)
                if _prev and not _prev.done():
                    logger.debug(f"Runner [{mode}]: previous task still running — skipping tick")
                    continue
                _t = asyncio.create_task(_run_one_mode(runner, bus, mode, _sym_override))
                _mode_tasks[mode] = _t


def _signal_to_dict(sig, mode: str) -> dict:
    # Compute R:R if entry, sl, tp are available
    from engine.account_store import current_mode as _acm
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
        "account_mode": _acm(),
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
        "regime":       sig.regime if hasattr(sig, "regime") else None,
    }


def start_runner_loop(client, order_manager, risk_manager) -> None:
    """Start the background strategy runner (idempotent)."""
    global _runner_task, _news_refresh_task, _trailing_stop_task, _risk_manager, _trailing_stop_manager
    _risk_manager = risk_manager
    if _runner_task is None or _runner_task.done():
        _runner_task = asyncio.create_task(
            _runner_loop(client, order_manager, risk_manager)
        )
        logger.info("Strategy runner task created.")
    if _news_refresh_task is None or _news_refresh_task.done():
        _news_refresh_task = asyncio.create_task(_news_refresh_loop())
        logger.info("News filter auto-refresh task created.")
    if _trailing_stop_task is None or _trailing_stop_task.done():
        from engine.trailing_stop import TrailingStopManager
        _trailing_stop_manager = TrailingStopManager(client, order_manager)
        _trailing_stop_task = asyncio.create_task(_trailing_stop_loop(_trailing_stop_manager))
        logger.info("Trailing stop monitor task created.")


async def _news_refresh_loop() -> None:
    """
    Background task that auto-refreshes the news filter cache.
    
    Runs every N minutes (configurable via risk.json news_filter.cache_minutes).
    Ensures the Forex Factory calendar stays current for the next 7-14 days
    to prevent trading during surprise high-impact news events.
    """
    from engine.news_filter import news_filter
    
    # Initial refresh on startup (non-blocking)
    logger.info("News filter: initial refresh on startup...")
    try:
        await asyncio.to_thread(news_filter._refresh)
        logger.info("News filter: initial refresh complete")
    except Exception as exc:
        logger.warning(f"News filter: initial refresh failed: {exc}")
    
    # Read cache interval from config (default 60 minutes)
    try:
        _risk_cfg = json.loads((CONFIG_DIR / "risk.json").read_text(encoding="utf-8"))
        _cache_minutes = _risk_cfg.get("news_filter", {}).get("cache_minutes", 60)
    except Exception:
        _cache_minutes = 60
    
    _interval_seconds = _cache_minutes * 60
    logger.info(f"News filter: auto-refresh every {_cache_minutes} minutes")
    
    while True:
        await asyncio.sleep(_interval_seconds)
        try:
            await asyncio.to_thread(news_filter._refresh)
            # news_filter._refresh() already logs event count, so no extra log needed
        except Exception as exc:
            logger.warning(f"News filter: auto-refresh failed: {exc}")


async def _trailing_stop_loop(manager) -> None:
    """
    Background task that monitors open positions and updates trailing stops.
    
    Runs every 5 seconds to check if any positions can have their SL moved
    to lock in profit. Only trails positions that have reached their
    activation threshold (e.g., 10+ pips profit for scalping).
    """
    logger.info("Trailing stop monitor: starting...")
    
    # Read config to check if trailing stops are enabled
    try:
        _app_cfg = json.loads((CONFIG_DIR / "app.json").read_text(encoding="utf-8"))
        _enabled = _app_cfg.get("trailing_stops", {}).get("enabled", False)
    except Exception:
        _enabled = False
    
    if not _enabled:
        logger.info("Trailing stops disabled in config — monitor inactive")
        return
    
    logger.info("Trailing stop monitor active (5-second tick)")
    
    while True:
        await asyncio.sleep(5)  # Check every 5 seconds
        
        if _paused:
            continue
        
        try:
            trailed_count = await asyncio.to_thread(manager.update_trailing_stops)
            if trailed_count > 0:
                logger.debug(f"Trailing stop: updated {trailed_count} position(s)")
        except Exception as exc:
            logger.warning(f"Trailing stop monitor error: {exc}")

