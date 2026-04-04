"""
Signal Bus — application-level singleton.

Ties together:
  - the in-memory signal queue (single source of truth, shared by HTTP routes
    and the strategy runner loop)
  - order execution when a signal is approved (manual) or auto-triggered (auto)
  - WebSocket broadcast so the dashboard stays in sync

Usage:
    from api.signal_bus import bus

    # At startup (main.py lifespan):
    bus.init(mt5_client, order_manager)

    # Runner loop / HTTP POST /signals/:
    await bus.add_signal(signal_dict)

    # HTTP POST /signals/{id}/approve:
    await bus.execute_signal(signal_id)
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_event_loop: Optional[asyncio.AbstractEventLoop] = None

# Signals older than this (seconds) with a terminal status are purged from the queue
_SIGNAL_TTL_SECONDS = 3600  # 1 hour
_TERMINAL_STATUSES = frozenset({"executed", "rejected", "failed", "expired"})

# Default expiry (seconds) per timeframe for PENDING manual signals
# One bar's worth of time — after this the signal's entry/SL/TP are considered stale
_DEFAULT_EXPIRY: dict[str, int] = {
    "M1":  60,
    "M5":  300,
    "M15": 900,
    "M30": 1800,
    "H1":  3600,
    "H4":  14400,
    "D1":  86400,
    "W1":  604800,
}

from loguru import logger

CONFIG_DIR = Path(__file__).parent.parent / "config"

# NEW-15: module-level TTL cache for app.json so _get_exec_mode / _is_ea_enabled
# don't read the file on every add_signal call (up to 200+ reads/min at high scan rate).
_app_cfg_cache_bus: dict = {}
_app_cfg_loaded_at_bus: float = 0.0
_APP_CFG_TTL_BUS = 5.0  # seconds — consistent with strategy_runner.py TTL


def _get_bus_app_cfg() -> dict:
    global _app_cfg_cache_bus, _app_cfg_loaded_at_bus
    now = time.monotonic()
    if now - _app_cfg_loaded_at_bus < _APP_CFG_TTL_BUS:
        return _app_cfg_cache_bus
    try:
        _app_cfg_cache_bus = json.loads((CONFIG_DIR / "app.json").read_text(encoding="utf-8-sig"))
    except Exception:
        pass  # return stale cache on read error
    _app_cfg_loaded_at_bus = now
    return _app_cfg_cache_bus


# Retrain dedup: track when each symbol×mode key last triggered an LSTM retrain.
# Prevents a burst of simultaneous _poll_outcome completions from queuing redundant
# OHLCV fetches and training jobs before predictor.is_training() is set.
_retrain_last_triggered: dict[str, float] = {}
_RETRAIN_DEDUP_SECS = 120   # 2-minute cooldown window per key

# Bar counts for auto-retrain (LSTM) — match run_retrain.py (MT5 per-request limit ~99k for M5)
_RETRAIN_BARS: dict[str, int] = {
    "scalping":    99_000,  # M5  ≈ 1 yr
    "day_trading": 17_000,  # H1  ≈ 2 yr
    "swing":        5_000,  # H4  ≈ 2 yr
}

# Bar counts for auto-optimizer triggers — same as manual (full 2-year dataset).
# Option A: Both manual and auto use 2-year data for robustness.
# Avoids overwriting robust long-term params with recent drawdown data.
# Frequency: 20 trades + 24hr cooldown ensures fast-enough adaptation without thrashing.
_AUTO_OPT_BARS: dict[str, int] = {
    "scalping":    99_000,  # M5  ≈ 2 years
    "day_trading": 17_000,  # H1  ≈ 2 years
    "swing":        5_000,  # H4  ≈ 2 years
}


def _set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup so the thread executor can schedule coroutines safely."""
    global _event_loop
    _event_loop = loop


class SignalBus:
    """
    Central hub for the full signal lifecycle:

    add_signal(signal)        → if auto-mode: execute immediately
                                if manual-mode: queue + broadcast to UI
    execute_signal(signal_id) → manually approved → place order → broadcast result
    """

    def __init__(self):
        # signal_id → signal dict  (shared with signals.py routes via bus.queue)
        self.queue: dict[str, dict] = {}
        # G-5: rolling archive of completed/expired/rejected signals (last 500)
        self.archive: deque[dict] = deque(maxlen=500)
        self._client = None
        self._order_manager = None
        # LOGIC-2: fast dedup set so concurrent add_signal calls within the same
        # event-loop tick cannot both pass the queue-scan guard.
        # Key = (symbol, strategy, direction, trading_mode). Cleared when the
        # signal reaches a terminal state or the key is evicted from the queue.
        self._pending_keys: set[tuple] = set()
        # NEW-6: track fire-and-forget execution tasks so they are not silently
        # dropped if the event loop is closed while a journal write is in-flight.
        self._active_tasks: set[asyncio.Task] = set()

    def init(self, client, order_manager) -> None:
        """Call once at API startup with live MT5 client + OrderManager."""
        self._client = client
        self._order_manager = order_manager

    def purge_stale(self) -> int:
        """Remove terminal-state signals older than _SIGNAL_TTL_SECONDS, and
        expire pending signals whose expires_at has passed. Returns count removed/expired."""
        now = datetime.now(tz=timezone.utc)
        to_delete = []
        for sid, sig in self.queue.items():
            # Expire stale terminal-state signals
            if sig.get("status") in _TERMINAL_STATUSES:
                try:
                    created = datetime.fromisoformat(sig["created_at"].replace("Z", "+00:00"))
                    if (now - created).total_seconds() > _SIGNAL_TTL_SECONDS:
                        to_delete.append(sid)
                except Exception:
                    to_delete.append(sid)
                continue
            # Expire pending signals past their expiry window
            if sig.get("status") == "pending" and sig.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(sig["expires_at"].replace("Z", "+00:00"))
                    if now >= exp:
                        sig["status"] = "expired"
                        sig["rejection_reason"] = "Signal expired — market conditions may have changed"
                        logger.info(f"Signal expired: #{sid[:8]} {sig.get('symbol')} {sig.get('strategy')}")
                        # LOGIC-2: release fast-dedup key on expiry
                        _ek = (sig.get("symbol"), sig.get("strategy"), sig.get("direction"), sig.get("trading_mode"))
                        self._pending_keys.discard(_ek)
                        # Broadcast expiry to dashboard
                        try:
                            if _event_loop and not _event_loop.is_closed():
                                asyncio.run_coroutine_threadsafe(
                                    self._broadcast_expired(dict(sig)), _event_loop
                                )
                        except Exception as _be:
                            logger.warning(f"Signal expiry broadcast failed: {_be}")
                except Exception:
                    pass
        for sid in to_delete:
            sig = self.queue.pop(sid, None)
            if sig:
                self.archive.append(dict(sig))
                # LOGIC-2: release the fast-dedup key so the same symbol/strategy
                # can be re-signalled after the previous attempt has settled.
                _k = (sig.get("symbol"), sig.get("strategy"), sig.get("direction"), sig.get("trading_mode"))
                self._pending_keys.discard(_k)
        if to_delete:
            logger.debug(f"SignalBus: purged {len(to_delete)} stale signals")
        return len(to_delete)

    async def _broadcast_expired(self, signal: dict) -> None:
        try:
            from api.websocket.feed import broadcast_signal
            await broadcast_signal({**signal, "type": "signal_update"})
        except Exception:
            pass

    # ── public API ──────────────────────────────────────────────────────────

    async def add_signal(self, signal: dict) -> dict:
        """
        Accept a new signal from the runner loop or HTTP POST /signals/.
        Auto-execution fires immediately if configured; otherwise queues for
        manual approval and pushes to the dashboard signal queue via WebSocket.
        """
        from api.websocket.feed import broadcast_signal

        # ── Dedup guard: skip if same symbol+direction+strategy+mode already active ──
        mode = signal.get("trading_mode", "")
        _dedup_key = (
            signal.get("symbol"), signal.get("strategy"),
            signal.get("direction"), mode,
        )
        # LOGIC-2: check the fast in-memory set FIRST — catches concurrent calls
        # within the same event-loop tick before the queue scan below.
        if _dedup_key in self._pending_keys:
            logger.debug(
                f"SignalBus: dedup (fast-set) dropped {signal.get('symbol')}/{signal.get('strategy')} ({mode})"
            )
            # IMPROVE-4: always surface why a signal was dropped
            signal["rejection_reason"] = "Duplicate signal already pending/executing"
            return signal
        for existing in self.queue.values():
            if existing.get("status") not in ("pending", "executing"):
                continue
            if (
                existing.get("symbol") == signal.get("symbol")
                and existing.get("direction") == signal.get("direction")
                and existing.get("strategy") == signal.get("strategy")
                and existing.get("trading_mode") == mode
            ):
                logger.debug(
                    f"SignalBus: dedup dropped {signal.get('symbol')}/{signal.get('strategy')} "
                    f"({mode}) — already {existing['status']}"
                )
                return existing

        # ── Open position guard: skip if bot already holds this symbol+direction
        #    in the SAME trading mode (different modes may run concurrently) ──
        try:
            if self._client is not None:
                open_positions = self._client.get_open_positions()
                sym = signal.get("symbol", "")
                direction = signal.get("direction", "").upper()
                mode_prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(mode, mode)
                for pos in open_positions:
                    if (
                        pos.get("symbol") == sym
                        and pos.get("type", "").upper() == direction
                        and str(pos.get("comment", "")).startswith(mode_prefix)
                    ):
                        logger.debug(
                            f"SignalBus: dedup dropped {sym}/{signal.get('strategy')} "
                            f"— open {direction} {mode} position already exists"
                        )
                        # IMPROVE-4: surface rejection reason
                        signal["rejection_reason"] = f"Open {direction} {mode} position already exists for {sym}"
                        return signal
        except Exception:
            pass

        exec_mode = self._get_exec_mode(mode)
        if exec_mode == "auto" and self._order_manager is not None:
            # Confidence floor for auto-execution — only enforced when
            # confidence_filter_enabled=true in app.json so users who disable
            # the confidence filter aren't silently blocked here too.
            # IMPORTANT: check BEFORE adding to the queue so repeated scanner
            # ticks (every 30 s) don't flood the queue/archive with rejected
            # entries and don't generate any notification noise on the dashboard.
            try:
                _conf_filter_on = bool(_get_bus_app_cfg().get("ai", {}).get("confidence_filter_enabled", True))
            except Exception:
                _conf_filter_on = True
            if _conf_filter_on:
                MIN_CONF = {"scalping": 0.60, "day_trading": 0.50, "swing": 0.45}
                min_conf = MIN_CONF.get(mode, 0.50)
                conf = float(signal.get("confidence") or 0.0)
                if conf < min_conf:
                    # Silently drop — do NOT queue, do NOT broadcast.
                    # Strategy runner generates a new UUID every scan tick so
                    # queuing these would fill the archive with thousands of
                    # rejected entries per hour and produce non-stop popups.
                    logger.debug(
                        f"SignalBus: conf-drop {signal.get('symbol')}/{signal.get('strategy')} "
                        f"({mode}) conf={conf:.0%} < {min_conf:.0%} — not queued"
                    )
                    # IMPROVE-4: record reason even for silent drops
                    signal["rejection_reason"] = (
                        f"Confidence {conf:.0%} below minimum {min_conf:.0%} for {mode}"
                    )
                    return signal
            signal["status"] = "executing"
            self.queue[signal["id"]] = signal
            self._pending_keys.add(_dedup_key)
            self.purge_stale()
            # RISK-5: synchronous circuit-breaker pre-check before creating the async
            # task.  _do_execute_sync repeats this check, but doing it here eliminates
            # the race window between task creation and the first line of the executor.
            try:
                from api.runner_loop import _risk_manager as _rm_pre_check
                if _rm_pre_check is not None:
                    _cb_ok, _cb_msg = _rm_pre_check.is_trading_allowed(mode or "day_trading")
                    if not _cb_ok:
                        signal["status"] = "rejected"
                        signal["rejection_reason"] = _cb_msg
                        self._pending_keys.discard(_dedup_key)
                        logger.info(f"SignalBus RISK-5 pre-check blocked: {_cb_msg}")
                        asyncio.create_task(broadcast_signal({**signal, "type": "signal_update"}))
                        return signal
            except Exception:
                pass
            # Broadcast immediately so the dashboard card appears before execution
            asyncio.create_task(broadcast_signal(dict(signal)))
            
            # Small delay for swing so UI renders the card before status flips
            # to "executed". Scalping/day_trading stay at 0 — speed matters there.
            async def _delayed_execute(sig: dict, delay: float) -> None:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._execute_async(sig)

            _exec_delay = 2.0 if mode == "swing" else 0.0
            _exec_task = asyncio.create_task(_delayed_execute(signal, _exec_delay))
            self._active_tasks.add(_exec_task)
            _exec_task.add_done_callback(self._active_tasks.discard)
        else:
            # Compute expiry for manual pending signals — keyed by timeframe, not mode
            try:
                exp_cfg = _get_bus_app_cfg().get("signal_expiry_seconds", {})
            except Exception:
                exp_cfg = {}
            tf = signal.get("timeframe", "")
            exp_secs = exp_cfg.get(tf) or _DEFAULT_EXPIRY.get(tf, 1800)
            from datetime import timedelta
            signal["expires_at"] = (
                datetime.now(tz=timezone.utc) + timedelta(seconds=exp_secs)
            ).isoformat()
            signal["status"] = "pending"
            self.queue[signal["id"]] = signal
            self._pending_keys.add(_dedup_key)
            self.purge_stale()
            # Push to every connected dashboard client
            asyncio.create_task(broadcast_signal(dict(signal)))

        return signal

    async def execute_signal(self, signal_id: str) -> dict:
        """
        Manually approved from the dashboard — execute the queued signal.
        Returns the signal dict immediately (execution happens in background).
        """
        signal = self.queue.get(signal_id)
        if signal is None:
            raise KeyError(signal_id)
        # GAP-5: reject stale signals at approval time, not just at queue time.
        expires_at = signal.get("expires_at")
        if expires_at:
            try:
                exp_dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if datetime.now(tz=timezone.utc) > exp_dt:
                    signal["status"] = "expired"
                    signal["rejection_reason"] = "Signal expired before manual approval"
                    raise ValueError(
                        f"Signal {signal_id} expired at {expires_at} — cannot execute"
                    )
            except ValueError:
                raise
            except Exception:
                pass  # malformed expires_at — allow execution
        # Status may already be "executing" if set by approve_signal under lock —
        # only update if it is still "pending" (direct call path).
        if signal.get("status") != "executing":
            signal["status"] = "executing"
        _exec_task = asyncio.create_task(self._execute_async(signal))
        self._active_tasks.add(_exec_task)
        _exec_task.add_done_callback(self._active_tasks.discard)
        return signal

    # ── internals ───────────────────────────────────────────────────────────

    async def _execute_async(self, signal: dict) -> None:
        from api.websocket.feed import broadcast_signal

        try:
            success = await asyncio.to_thread(self._do_execute_sync, signal)
            signal["status"] = "executed" if success else "failed"
            signal["actioned_at"] = datetime.now(tz=timezone.utc).isoformat()
        except Exception as exc:
            logger.exception(f"SignalBus execution error: {exc}")
            signal["status"] = "failed"
            signal["error"] = str(exc)
            # IMPROVE-4: ensure rejection_reason is set so dashboard can display it
            if not signal.get("rejection_reason"):
                signal["rejection_reason"] = str(exc)

        # Notify dashboard of the outcome
        await broadcast_signal({**signal, "type": "signal_update"})

    def _do_execute_sync(self, signal: dict) -> bool:
        """Synchronous order placement — runs in a thread executor."""
        if self._order_manager is None or self._client is None:
            # IMPROVE-4: populate rejection_reason so dashboard shows why
            signal["rejection_reason"] = "SignalBus not initialised — order manager or MT5 client missing"
            logger.error("SignalBus: not initialised — cannot execute order")
            return False

        from engine.order_manager import OrderRequest

        # Circuit-breaker + concurrent/per-symbol limit check at execution time
        try:
            from engine.mt5_client import MT5Client
            from api.runner_loop import _risk_manager
            if _risk_manager is not None:
                trading_mode_check = signal.get("trading_mode", "day_trading")

                # ── Circuit breaker check (enabled flag + drawdown halts) ──────
                cb_allowed, cb_reason = _risk_manager.is_trading_allowed(trading_mode_check)
                if not cb_allowed:
                    signal["rejection_reason"] = cb_reason
                    logger.info(f"SignalBus blocked by circuit breaker: {cb_reason}")
                    return False

                # ── Concurrent + per-symbol limit check ───────────────────────
                if isinstance(self._client, MT5Client):
                    open_positions = self._client.get_open_positions()
                    allowed, reason = _risk_manager.check_concurrent_limit(
                        trading_mode_check,
                        open_positions,
                        symbol=signal.get("symbol"),
                    )
                    if not allowed:
                        signal["rejection_reason"] = reason
                        logger.info(f"SignalBus blocked execution: {reason}")
                        return False
        except Exception as _exc:
            logger.debug(f"SignalBus: risk checks skipped: {_exc}")

        try:
            trading_mode = signal.get("trading_mode", "day_trading")
            mode_prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(trading_mode, "bot")

            # ── MQL5 EA fast-path for scalping ────────────────────────────────
            # If the AIBotScalper EA is running and ea_enabled=true in app.json,
            # dispatch scalping orders through the EA (1–5 ms native execution).
            # IMPROVE-3: Python fallback is ALWAYS reachable — ea_used stays False
            # unless EA explicitly returns success.  Fallback triggers automatically
            # on: EA not active, heartbeat stale, command-file write error, result
            # timeout, or any unhandled exception inside the try block below.
            ea_used = False
            if trading_mode == "scalping" and self._is_ea_enabled():
                try:
                    from engine.ea_bridge import ea_bridge
                    if ea_bridge.is_active():
                        ea_used = ea_bridge.submit_and_wait(signal, timeout=5.0)
                        if ea_used:
                            logger.info(
                                f"Signal executed via EA: {signal['symbol']} {signal['direction']} "
                                f"lot={signal.get('lot_size')} ticket={signal.get('ticket')}"
                            )
                        else:
                            logger.info(
                                f"EA execution failed/timeout for {signal['symbol']} "
                                "— falling back to Python order"
                            )
                    else:
                        logger.debug("EABridge: EA not active — using Python execution")
                except Exception as _ea_exc:
                    logger.warning(f"EABridge: error during EA dispatch: {_ea_exc} — falling back")

            if ea_used:
                # EA already filled the order — journal and continue
                req_volume = float(signal.get("lot_size") or 0.01)
                ticket = signal.get("ticket")
                fill_price = signal.get("fill_price")
                try:
                    from engine.trade_journal import trade_journal
                    from engine.account_store import current_mode
                    trade_journal.log(
                        ticket=ticket or 0,
                        symbol=signal["symbol"],
                        direction=signal["direction"],
                        volume=req_volume,
                        entry=fill_price or 0.0,
                        sl=float(signal.get("sl") or 0),
                        tp=float(signal["tp"]) if signal.get("tp") else None,
                        profit=None,
                        trading_type=trading_mode,
                        account_mode=current_mode(),
                        comment=signal.get("strategy", ""),
                        event="open",
                        confidence=float(signal.get("confidence") or 0.5),
                    )
                except Exception:
                    pass
                if ticket is not None and _event_loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        _poll_outcome(ticket=ticket, signal=signal, client=self._client),
                        _event_loop,
                    )
                return True
            # ── Standard Python execution (day_trading / swing / EA fallback) ─

            # entry_price: runner_loop uses key "entry_price"; HTTP route uses "entry".
            # Read both so SL/TP reanchoring fires regardless of origin.
            _ep_raw = signal.get("entry_price") or signal.get("entry")

            # LOGIC-3: revalidate lot size using current balance before placing order.
            # This matters for pending manual signals that may be minutes or hours old.
            _lot = float(signal.get("lot_size") or 0.01)
            try:
                from api.runner_loop import _risk_manager as _rm_lot
                if _rm_lot is not None and hasattr(self._client, "get_account_info"):
                    _acct_lot = self._client.get_account_info()
                    if _acct_lot and _acct_lot.get("balance"):
                        _cur_bal = float(_acct_lot["balance"])
                        _sym_info_lot = self._client.get_symbol_info(signal["symbol"])
                        _sl_lot = float(signal.get("sl") or 0)
                        _ep_lot = float(_ep_raw) if _ep_raw else 0.0
                        if _sl_lot and _ep_lot and _sym_info_lot:
                            _new_lot = _rm_lot.calculate_lot_size(
                                balance=_cur_bal,
                                entry=_ep_lot,
                                sl=_sl_lot,
                                tick_value=_sym_info_lot.get("pip_value", 1.0),
                                tick_size=_sym_info_lot.get("tick_size", 0.00001),
                                min_lot=_sym_info_lot.get("volume_min", 0.01),
                                max_lot=_sym_info_lot.get("volume_max", 100.0),
                                lot_step=_sym_info_lot.get("volume_step", 0.01),
                            )
                            if abs(_new_lot - _lot) / max(_lot, 1e-8) > 0.10:
                                logger.info(
                                    f"Lot revalidated for stale signal {signal.get('id','?')}: "
                                    f"{_lot} → {_new_lot} (balance={_cur_bal:.2f})"
                                )
                                _lot = _new_lot
                                signal["lot_size"] = _new_lot
            except Exception as _lot_exc:
                logger.debug(f"SignalBus: lot revalidation skipped: {_lot_exc}")

            # RISK-1: reject signal when lot size is 0 (returned by calculate_lot_size
            # when SL distance is zero — trading with an invalid lot wastes risk budget).
            if _lot <= 0:
                err = "Lot size is zero — SL distance is zero or invalid; signal rejected"
                signal["rejection_reason"] = err
                logger.error(f"SignalBus: {err} ({signal.get('symbol')} {signal.get('direction')})")
                return False

            req = OrderRequest(
                symbol=signal["symbol"],
                direction=signal["direction"].upper(),
                volume=_lot,
                sl=float(signal["sl"]),
                tp=float(signal["tp"]) if signal.get("tp") else None,
                entry_price=float(_ep_raw) if _ep_raw else None,
                comment=f"{mode_prefix}|{signal.get('strategy', '?')[:20]}",
            )
            result = self._order_manager.place_market_order(req)
            if result and result.success:
                signal["ticket"] = result.ticket
                signal["fill_price"] = result.open_price
                logger.info(
                    f"Signal executed: {signal['symbol']} {signal['direction']} "
                    f"lot={req.volume} ticket={result.ticket}"
                )
                # Journal: record trade open
                try:
                    from engine.trade_journal import trade_journal
                    from engine.account_store import current_mode
                    trade_journal.log(
                        ticket=result.ticket,
                        symbol=signal["symbol"],
                        direction=signal["direction"],
                        volume=req.volume,
                        entry=result.open_price or 0.0,
                        sl=float(signal.get("sl") or 0),
                        tp=float(signal["tp"]) if signal.get("tp") else None,
                        profit=None,
                        trading_type=signal.get("trading_mode", "day_trading"),
                        account_mode=current_mode(),
                        comment=signal.get("strategy", ""),
                        event="open",
                        confidence=float(signal.get("confidence") or 0.5),
                    )
                except Exception:
                    pass
                # Phase 7: kick off outcome poller in background
                ticket = result.ticket
                if ticket is not None and _event_loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        _poll_outcome(
                            ticket=ticket,
                            signal=signal,
                            client=self._client,
                        ),
                        _event_loop,
                    )
                return True
            else:
                err = result.error if result else "Order manager returned no result"
                signal["rejection_reason"] = err
                logger.warning(f"Signal execution failed: {signal['symbol']} {signal['direction']} — {err}")
                return False
        except Exception as exc:
            # IMPROVE-4: populate rejection_reason so the dashboard card shows a cause
            signal["rejection_reason"] = f"Execution error: {exc}"
            logger.exception(f"_do_execute_sync error: {exc}")
            return False

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _get_exec_mode(trading_type: str) -> str:
        # NEW-15: use TTL-cached config read instead of raw file read per call
        try:
            cfg = _get_bus_app_cfg()
            return cfg.get("execution_mode", {}).get(trading_type, "manual")
        except Exception:
            return "manual"

    @staticmethod
    def _is_ea_enabled() -> bool:
        """Return True if ea_enabled=true in app.json."""
        # NEW-15: use TTL-cached config read instead of raw file read per call
        try:
            cfg = _get_bus_app_cfg()
            return bool(cfg.get("ea_enabled", False))
        except Exception:
            return False


# ── outcome poller ────────────────────────────────────────────────────────────

def _get_server_utc_offset_secs(symbol: str = "EURUSD") -> int:
    """
    Detect how many seconds the MT5 server clock is ahead of true UTC.
    Many brokers (e.g. XM EET = UTC+2) return deal/tick timestamps that are
    offset from the Unix epoch by their local timezone.  We detect this by
    comparing a fresh tick timestamp with the Python wall clock and rounding
    to the nearest hour.

    IMPORTANT: always probe liquid 24/5 forex pairs — NEVER the trading symbol.
    Commodities (SILVER, XAUUSD) and indices (US30) have market hours; their
    tick.time can be stale by hours or days when the market is closed, which
    causes the offset calculation to return a wildly wrong value and shifts
    every close_time by the same amount (e.g. +48 h for a 2-day-old tick).

    Returns 0 if no fresh tick is available (safe default — deal.time is used as-is).
    """
    import MetaTrader5 as mt5
    import time as _time

    # Always use a 24/5 forex pair — these always have a fresh tick during market hours.
    # The `symbol` parameter is intentionally ignored to prevent stale commodity ticks.
    _FOREX_PROBES = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF")
    tick = None
    for probe in _FOREX_PROBES:
        tick = mt5.symbol_info_tick(probe)
        if tick is not None:
            break
    if tick is None:
        return 0
    diff = tick.time - int(_time.time())
    # Sanity guard: the maximum real-world UTC offset is ±14 h (UTC+14).
    # If |diff| exceeds this, even a forex tick is stale (weekend / holiday) — return 0.
    if abs(diff) > 14 * 3600:
        return 0
    # Round to nearest hour — broker offsets are always whole hours
    return round(diff / 3600) * 3600


async def recover_unclosed_trades(client) -> None:
    """
    Called once at startup to backfill close journal entries and trade
    memory records for positions that were closed while the server was
    offline (i.e. their _poll_outcome task was lost on restart).
    """
    import MetaTrader5 as mt5
    from engine.trade_journal import trade_journal
    from ai.trade_memory import memory, TradeOutcome
    from ai.rl_agent import rl_manager
    from engine.account_store import current_mode

    unclosed = trade_journal.get_unclosed_tickets()
    if not unclosed:
        return

    logger.info(f"Recovery: found {len(unclosed)} unclosed journal ticket(s): "
                f"{[e['ticket'] for e in unclosed]}")

    # Check which are still live in MT5 (use client method to respect MT5Client._lock)
    live_raw = await asyncio.to_thread(client.get_open_positions)
    live_tickets = {p["ticket"] for p in (live_raw or [])}

    # Detect server-clock UTC offset once (e.g. EET = +7200 s)
    server_offset = await asyncio.to_thread(_get_server_utc_offset_secs)
    logger.info(f"Recovery: detected server UTC offset = {server_offset // 3600:+d}h")

    for entry in unclosed:
        ticket = entry["ticket"]
        if ticket in live_tickets:
            continue  # still open — _poll_outcome will handle it (re-launched below)

        # Position is gone — look it up in deal history by position ID
        # Use client method to respect MT5Client._lock
        deals = await asyncio.to_thread(client.get_deals_by_position, ticket)
        if not deals:
            deals = []

        # Resolve symbol early so it's available in all log/warning messages below
        symbol = entry.get("symbol", "")

        closed = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        if not closed:
            logger.warning(f"Recovery: no close deal found for ticket #{ticket} ({symbol}) — skipping")
            continue

        deal      = closed[-1]
        # Correct deal.time for broker server-clock offset so we store true UTC
        close_time = datetime.fromtimestamp(deal.time - server_offset, tz=timezone.utc).isoformat()
        profit     = deal.profit
        close_px   = deal.price
        entry_px   = float(entry.get("entry") or 0)
        direction  = entry.get("direction", "buy").upper()
        trading_type = entry.get("trading_mode") or entry.get("trading_type") or "day_trading"
        pip_val    = 0.01 if "JPY" in symbol else 0.0001
        pips       = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_val

        sl = float(entry.get("sl") or 0)
        tp = float(entry.get("tp") or 0)
        # Same tol + direction-aware logic as _poll_outcome (0.01% of price, min 2 pips).
        # The old tol=pip_val*3 had no profit fallback so ATR-trailed closes were
        # always labelled manual_close regardless of whether they were wins or losses.
        tol = max(abs(close_px) * 0.0001, pip_val * 2)
        if direction == "BUY":
            if tp and close_px >= tp - tol:
                outcome_type = "tp_hit"
            elif sl and close_px <= sl + tol:
                outcome_type = "sl_hit"
            elif profit > 0:
                outcome_type = "tp_hit"
            elif profit < 0:
                outcome_type = "sl_hit"
            else:
                outcome_type = "manual_close"
        else:  # SELL
            if tp and close_px <= tp + tol:
                outcome_type = "tp_hit"
            elif sl and close_px >= sl - tol:
                outcome_type = "sl_hit"
            elif profit > 0:
                outcome_type = "tp_hit"
            elif profit < 0:
                outcome_type = "sl_hit"
            else:
                outcome_type = "manual_close"

        open_dt2  = datetime.fromisoformat(entry.get("open_time", close_time).replace("Z", "+00:00"))
        # Use the offset-corrected close_time ISO string (not raw deal.time) so both
        # endpoints share the same UTC reference and duration is calculated correctly.
        close_dt2 = datetime.fromisoformat(close_time)
        dur_mins  = (close_dt2 - open_dt2).total_seconds() / 60

        # Write close event to journal
        trade_journal.log(
            ticket=ticket,
            symbol=symbol,
            direction=entry.get("direction", "buy"),
            volume=float(entry.get("volume") or 0.01),
            entry=entry_px,
            sl=sl,
            tp=tp if tp else None,
            profit=profit,
            trading_type=trading_type,
            account_mode=entry.get("account_mode") or current_mode(),
            comment=entry.get("comment", ""),
            event="close",
            close_time=close_time,
        )

        # Feed to trade memory and RL
        try:
            # Compute profit_pct so trade memory never stores 0.0 for recovered trades
            try:
                from engine.risk_manager import risk_manager as _rm_rec2
                _rec2_bal = _rm_rec2._day_start_balance or 0.0
            except Exception:
                _rec2_bal = 0.0
            _rec2_pct = (profit / _rec2_bal * 100.0) if _rec2_bal > 0 else (profit / 10000.0 * 100.0)

            outcome = TradeOutcome(
                ticket=ticket,
                symbol=symbol,
                strategy=entry.get("comment", "unknown"),
                trading_type=trading_type,
                direction=direction,
                confidence=float(entry.get("confidence") or 0.5),
                entry_price=entry_px,
                close_price=close_px,
                sl_price=sl,
                tp_price=tp,
                volume=float(entry.get("volume") or 0.01),
                profit=profit,
                profit_pips=round(pips, 1),
                profit_pct=_rec2_pct,
                outcome=outcome_type,
                open_time=entry.get("open_time", close_time),
                close_time=close_time,
                duration_mins=round(dur_mins, 1),
                mode=entry.get("account_mode") or current_mode(),
                lstm_predicted_direction=direction,
                extra={"source": "live", "slippage_pips": 0.0},
            )
            memory.record(outcome)
            _stats = memory.stats(trading_type=trading_type, live_only=True)
            # Normalize raw dollar profit → % of balance (same scale as _poll_outcome)
            try:
                from engine.risk_manager import risk_manager as _rm_rec
                _rec_bal = _rm_rec._day_start_balance or 0.0
            except Exception:
                _rec_bal = 0.0
            _rec_pct = (profit / _rec_bal * 100.0) if _rec_bal > 0 else (profit / 10000.0 * 100.0)
            rl_manager.on_trade_closed(
                trading_type=trading_type,
                profit_pct=_rec_pct,
                win_rate=_stats.get("win_rate", 0.5),
                avg_conf=_stats.get("avg_conf", 0.5),
            )
        except Exception as _exc:
            logger.debug(f"Recovery: trade memory record failed for #{ticket}: {_exc}")

        logger.info(f"Recovery: backfilled close for #{ticket} {symbol} {direction} profit={profit:.2f}")

    # Re-launch _poll_outcome for any tickets that are still live
    still_open = [e for e in unclosed if e["ticket"] in live_tickets]
    for entry in still_open:
        fake_signal = {
            "symbol":       entry.get("symbol"),
            "direction":    entry.get("direction", "buy"),
            "trading_mode": entry.get("trading_mode") or entry.get("trading_type") or "day_trading",
            "strategy":     entry.get("comment", ""),
            "fill_price":   entry.get("entry"),
            "entry_price":  entry.get("entry"),
            "sl":           entry.get("sl"),
            "tp":           entry.get("tp"),
            "lot_size":     entry.get("volume"),
            "confidence":   float(entry.get("confidence") or 0.5),
        }
        asyncio.create_task(
            _poll_outcome(ticket=entry["ticket"], signal=fake_signal, client=client)
        )
        logger.info(f"Recovery: re-launched poll for still-open #{entry['ticket']} {entry.get('symbol')}")

    # BUG-4 / GAP-8: reverse reconciliation — find MT5 live positions that have
    # NO journal "open" entry (opened before the bot started, externally opened,
    # or from a prior run that never wrote the open-event).  Without this check
    # such positions would never be polled and their close would be silently missed.
    journal_open_tickets = {e["ticket"] for e in unclosed}
    untracked = [p for p in (live_raw or []) if p["ticket"] not in journal_open_tickets]
    for pos in untracked:
        _ticket  = pos["ticket"]
        _symbol  = pos.get("symbol", "")
        _dir     = "buy" if pos.get("type", "").upper() in ("BUY", "0") else "sell"
        _entry   = pos.get("open_price") or pos.get("price_open") or 0.0
        _sl      = pos.get("sl") or 0.0
        _tp      = pos.get("tp") or 0.0
        _volume  = pos.get("volume") or 0.01
        _comment = pos.get("comment", "")
        # NEW-13: detect trading type from the comment prefix (scalp|, day|, swing|)
        # so the correct RL agent is updated and the correct MAX_POLLS budget is used.
        _cmt_lower = _comment.lower()
        if _cmt_lower.startswith("scalp"):
            _trading_type_untracked = "scalping"
        elif _cmt_lower.startswith("swing"):
            _trading_type_untracked = "swing"
        else:
            _trading_type_untracked = "day_trading"
        # Write an open-event so future restarts see this position in the journal
        try:
            trade_journal.log(
                ticket=_ticket,
                symbol=_symbol,
                direction=_dir,
                volume=float(_volume),
                entry=float(_entry),
                sl=float(_sl),
                tp=float(_tp) if _tp else None,
                profit=None,
                trading_type=_trading_type_untracked,
                account_mode=current_mode(),
                comment=_comment,
                event="open",
            )
        except Exception as _jw:
            logger.debug(f"Recovery: journal write failed for untracked #{_ticket}: {_jw}")
        fake_signal = {
            "symbol":       _symbol,
            "direction":    _dir,
            "trading_mode": _trading_type_untracked,
            "strategy":     _comment,
            "fill_price":   float(_entry),
            "entry_price":  float(_entry),
            "sl":           float(_sl),
            "tp":           float(_tp),
            "lot_size":     float(_volume),
            "confidence":   0.5,
        }
        asyncio.create_task(
            _poll_outcome(ticket=_ticket, signal=fake_signal, client=client)
        )
        logger.info(
            f"Recovery: found untracked live position #{_ticket} {_symbol} {_dir} "
            f"— wrote journal open-event and launched poll"
        )


async def _poll_outcome(ticket: int, signal: dict, client) -> None:
    """
    Poll MT5 every 30 s until the position is closed, then record
    the outcome in TradeMemory and feed the result to the RL agent.

    Stops polling after 2 days (scalping), 14 days (day trading), or 45 days (swing).
    """
    import MetaTrader5 as mt5
    from ai.trade_memory import memory, TradeOutcome
    from ai.rl_agent import rl_manager

    # NEW-5: per-trading-type poll budget (30 s intervals)
    # scalping: 2 days, day_trading: 14 days, swing: 45 days
    _MAX_POLLS_BY_TYPE = {"scalping": 2*24*120, "day_trading": 14*24*120, "swing": 45*24*120}
    MAX_POLLS  = _MAX_POLLS_BY_TYPE.get(signal.get("trading_mode", "day_trading"), 14*24*120)
    open_time  = datetime.now(tz=timezone.utc).isoformat()

    # H-6 fix: fetch broker server-clock UTC offset ONCE per trade (it's a session constant).
    # Re-fetching it inside the 30 s poll loop wasted a live tick request per iteration.
    _srv_offset = await asyncio.to_thread(_get_server_utc_offset_secs, signal.get("symbol", "EURUSD"))

    # Fetch symbol digits ONCE — used to round SL to the instrument's correct precision
    # (5 for EURUSD, 3 for USDJPY, 2 for XAUUSD/BTCUSD, etc.)
    # NEW-14: use client.get_symbol_info() which acquires MT5Client._lock, consistent
    # with the lock pattern enforced everywhere else (fixes same issue as NEW-11).
    _sym_digits = 5
    try:
        _si_dict = await asyncio.to_thread(client.get_symbol_info, signal.get("symbol", "EURUSD"))
        if _si_dict:
            _sym_digits = int(_si_dict.get("digits", 5))
    except Exception:
        pass

    # G-6: tp2 partial-close state — set to True after tp1 partial-close fires
    _tp1_triggered = False
    # Swing: set to True once SL has been moved to breakeven
    _swing_be_triggered = False
    # ATR cache: keyed by timeframe string → (atr_value, monotonic_timestamp)
    # Refreshed at most once every 5 minutes — ATR on H1/H4 changes per candle close
    # not per 30-second poll, so re-fetching every cycle is wasteful.
    _atr_cache: dict = {}

    for _ in range(MAX_POLLS):
        await asyncio.sleep(30)
        try:
            # Position still open?
            # NEW-14: use client.get_position_by_ticket() which acquires MT5Client._lock
            # instead of calling mt5.positions_get() directly (consistent with NEW-11 fix).
            pos = await asyncio.to_thread(client.get_position_by_ticket, ticket)
            if pos:
                direction = signal.get("direction", "").upper()
                entry_px  = float(signal.get("fill_price") or signal.get("entry_price", 0))
                orig_sl   = float(signal.get("sl", 0))
                tp2       = float(signal.get("tp2") or 0)
                tp1       = float(signal.get("tp") or 0)

                def _get_om():
                    from api.main import get_mt5_client as _gmc
                    from engine.order_manager import OrderManager as _OMx
                    _cx = _gmc()
                    return _OMx(_cx) if _cx else None

                async def _try_trail(new_sl: float) -> None:
                    """Move SL only if it improves (never widen the stop).
                    Runs modify_position via asyncio.to_thread so the event loop
                    is not blocked by the blocking MT5 SDK call."""
                    om = _get_om()
                    if not om:
                        return
                    _rounded = round(new_sl, _sym_digits)
                    if direction == "BUY" and _rounded > pos.sl + 1e-9:
                        await asyncio.to_thread(om.modify_position, ticket, _rounded)
                        logger.info(f"Trail SL → {_rounded} | #{ticket} {signal.get('symbol')}")
                    elif direction == "SELL" and (pos.sl == 0 or _rounded < pos.sl - 1e-9):
                        await asyncio.to_thread(om.modify_position, ticket, _rounded)
                        logger.info(f"Trail SL → {_rounded} | #{ticket} {signal.get('symbol')}")

                async def _get_atr(timeframe: str, period: int = 14) -> float:
                    """
                    Compute ATR(period) for this symbol on `timeframe`.
                    True Range = max(H-L, |H-prev_close|, |L-prev_close|).
                    Smoothed with a simple rolling mean (SMA-ATR).
                    Result is cached per timeframe for 5 minutes — H1/H4 ATR
                    only changes when a new candle closes, so re-fetching every
                    30 s poll cycle is entirely wasteful.
                    Returns the ATR value, or 0.0 on any failure.
                    """
                    import pandas as pd
                    _now_mono = time.monotonic()
                    _cached = _atr_cache.get(timeframe)
                    if _cached and _now_mono - _cached[1] < 300:  # 5-minute cache
                        return _cached[0]
                    try:
                        _df = await asyncio.to_thread(
                            client.get_ohlcv, signal.get("symbol", ""), timeframe, period + 6
                        )
                        if _df is None or len(_df) < period + 1:
                            return 0.0
                        high = _df["high"]
                        low  = _df["low"]
                        prev = _df["close"].shift(1)
                        tr   = pd.concat([
                            high - low,
                            (high - prev).abs(),
                            (low  - prev).abs(),
                        ], axis=1).max(axis=1)
                        val  = float(tr.rolling(period).mean().iloc[-1])
                        val  = val if val > 0 else 0.0
                        _atr_cache[timeframe] = (val, _now_mono)
                        return val
                    except Exception:
                        return 0.0

                # ── Day trading: TP1 partial-close + move SL to breakeven ────
                if tp2 and tp1 and not _tp1_triggered:
                    hit_tp1 = (
                        (direction == "BUY"  and pos.price_current >= tp1) or
                        (direction == "SELL" and pos.price_current <= tp1)
                    )
                    if hit_tp1:
                        _tp1_triggered = True
                        try:
                            om = _get_om()
                            if om and entry_px:
                                await asyncio.to_thread(om.partial_close, ticket, 0.5)
                                await asyncio.to_thread(om.modify_position, ticket, entry_px, tp2)
                                logger.info(
                                    f"TP1 partial-close fired: #{ticket} {signal.get('symbol')} "
                                    f"BE={entry_px} → targeting TP2={tp2}"
                                )
                        except Exception as _pce:
                            logger.warning(f"TP1 partial-close failed #{ticket}: {_pce}")

                # ── Day trading: ATR trail on remainder after TP1 ────────────
                # Trail distance = ATR(14, H1) × 1.5 — adapts to current intraday
                # volatility so wide candles don't stop out the runner prematurely.
                # Falls back to 50% original-SL distance if MT5 data is unavailable.
                if _tp1_triggered and entry_px and orig_sl:
                    try:
                        _atr = await _get_atr("H1", 14)
                        _trail_dist = (_atr * 1.5) if _atr > 0 else abs(entry_px - orig_sl) * 0.5
                        _new_sl = (
                            max(pos.price_current - _trail_dist, entry_px) if direction == "BUY"
                            else min(pos.price_current + _trail_dist, entry_px)
                        )
                        await _try_trail(_new_sl)
                    except Exception as _te:
                        logger.debug(f"Day trail failed #{ticket}: {_te}")

                # ── Swing: BE at halfway then ATR trail (H4 ATR × 2.0) ───────
                # Swing signals have tp2=None so tp2==0 here.
                # Step 1: once price reaches 50% of TP distance, move SL to entry.
                # Step 2: after BE fires, trail with ATR(14, H4) × 2.0 — wider buffer
                #         to survive multi-day pullbacks without premature stop-outs.
                #         Falls back to 50% original-SL distance if ATR fetch fails.
                elif not tp2 and tp1 and entry_px and orig_sl:
                    _halfway = (
                        entry_px + (tp1 - entry_px) * 0.5 if direction == "BUY"
                        else entry_px - (entry_px - tp1) * 0.5
                    )
                    if not _swing_be_triggered:
                        hit_halfway = (
                            (direction == "BUY"  and pos.price_current >= _halfway) or
                            (direction == "SELL" and pos.price_current <= _halfway)
                        )
                        if hit_halfway:
                            _swing_be_triggered = True
                            try:
                                om = _get_om()
                                if om and entry_px:
                                    await asyncio.to_thread(om.modify_position, ticket, entry_px)
                                    logger.info(
                                        f"Swing BE fired: #{ticket} {signal.get('symbol')} "
                                        f"SL → entry {entry_px}"
                                    )
                            except Exception as _be_e:
                                logger.warning(f"Swing BE failed #{ticket}: {_be_e}")
                    if _swing_be_triggered:
                        try:
                            _atr = await _get_atr("H4", 14)
                            _trail_dist = (_atr * 2.0) if _atr > 0 else abs(entry_px - orig_sl) * 0.5
                            _new_sl = (
                                max(pos.price_current - _trail_dist, entry_px) if direction == "BUY"
                                else min(pos.price_current + _trail_dist, entry_px)
                            )
                            await _try_trail(_new_sl)
                        except Exception as _te:
                            logger.debug(f"Swing trail failed #{ticket}: {_te}")

                continue   # still open

            # Look in history by position ID — use the client method which has a
            # date-range fallback for brokers that require history pre-loading.
            deals = await asyncio.to_thread(client.get_deals_by_position, ticket)
            if deals is None:
                deals = []

            closed = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
            if not closed:
                continue

            deal       = closed[-1]
            # Use Python clock for close_time — avoids broker server-clock offset issues.
            # deal.time can be in local server time (e.g. EET = UTC+2) on some brokers.
            profit     = deal.profit
            close_px   = deal.price
            entry_px   = float(signal.get("fill_price") or signal.get("entry_price", 0))
            sym        = signal["symbol"]
            pip_val    = 0.0001 if "JPY" not in sym else 0.01
            # Outcome-matching tolerance: max(0.01% of price, 2 pips).
            # 0.001 (0.1%) was too wide — for EURJPY at ~184 it yielded 18.4 pips,
            # which exceeded the SL-TP gap on tight scalping setups and caused SL
            # hits to be mislabelled as TP hits.  0.0001 (0.01%) is sufficient to
            # absorb normal broker slippage on all instrument classes:
            #   forex (~1.10)  → max(0.00011, 0.0002) = 0.0002  (2 pips)
            #   JPY   (~184)   → max(0.01840, 0.0200) = 0.0200  (2 pips)
            #   gold  (~4400)  → max(0.44,   0.0002) = 0.44
            #   ETH   (~2000)  → max(0.20,   0.0002) = 0.20
            tol = max(abs(close_px) * 0.0001, pip_val * 2)
            direction  = signal["direction"].upper()
            pips       = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_val
            # Use deal.time corrected for broker server-clock offset to store true UTC
            # _srv_offset was fetched once before the loop (H-6 fix)
            close_time = datetime.fromtimestamp(deal.time - _srv_offset, tz=timezone.utc).isoformat()

            # Determine outcome type using direction-aware comparison.
            # Bidirectional abs() checks were also wrong: for a BUY, a TP check
            # should only fire when close_px is AT OR ABOVE the target, not below.
            sl = float(signal.get("sl", 0))
            tp = float(signal.get("tp") or 0)
            tp2_sig = float(signal.get("tp2") or 0)
            # Day trading: after TP1 partial-close fires the runner targets tp2.
            # _tp1_triggered carries over from the polling loop above.
            _check_tp = tp2_sig if _tp1_triggered and tp2_sig else tp
            if direction == "BUY":
                if _check_tp and close_px >= _check_tp - tol:
                    outcome_type = "tp_hit"
                elif sl and close_px <= sl + tol:
                    outcome_type = "sl_hit"
                elif profit > 0:
                    outcome_type = "tp_hit"
                elif profit < 0:
                    outcome_type = "sl_hit"
                else:
                    outcome_type = "manual_close"
            else:  # SELL
                if _check_tp and close_px <= _check_tp + tol:
                    outcome_type = "tp_hit"
                elif sl and close_px >= sl - tol:
                    outcome_type = "sl_hit"
                elif profit > 0:
                    outcome_type = "tp_hit"
                elif profit < 0:
                    outcome_type = "sl_hit"
                else:
                    outcome_type = "manual_close"

            # H-1 fix: derive close_dt from close_time (already broker-offset-corrected)
            # rather than raw deal.time so both endpoints share the same UTC clock.
            open_dt  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_time)
            dur_mins = (close_dt - open_dt).total_seconds() / 60

            # GAP-2: derive profit_pct for trade memory using last-known day-start balance
            # (a fresh balance fetch happens later for RL; this provisional value ensures
            # analytics / win-rate pct columns are never zero)
            try:
                from api.runner_loop import _risk_manager as _rm_pre
                _pre_bal = _rm_pre._day_start_balance if _rm_pre else None
            except Exception:
                _pre_bal = None
            _mem_profit_pct = (profit / _pre_bal * 100.0) if _pre_bal and _pre_bal > 0 else (profit / 10_000.0 * 100.0)

            # GAP-6: compute execution slippage = |fill_price - signal_entry_price| in pips.
            # fill_price is set by _do_execute_sync after MT5 order fill.
            # Stored in extra for analytics and future RL feature use.
            _signal_entry = float(signal.get("entry_price") or signal.get("entry") or 0.0)
            _fill         = float(signal.get("fill_price") or 0.0)
            _slippage_pips: float = 0.0
            if _signal_entry > 0 and _fill > 0:
                _slippage_pips = round(abs(_fill - _signal_entry) / pip_val, 1)
                if _slippage_pips > 0:
                    logger.debug(
                        f"Slippage #{ticket} {signal.get('symbol')}: "
                        f"signal={_signal_entry} fill={_fill} → {_slippage_pips} pips"
                    )

            try:
                from engine.account_store import current_mode as _poll_cm
                _poll_mode = _poll_cm()
            except Exception:
                _poll_mode = "live"

            outcome = TradeOutcome(
                ticket=ticket,
                symbol=signal["symbol"],
                strategy=signal.get("strategy", "unknown"),
                trading_type=signal.get("trading_mode", "day_trading"),
                direction=direction,
                confidence=float(signal.get("confidence") or 0.5),
                entry_price=entry_px,
                close_price=close_px,
                sl_price=sl,
                tp_price=_check_tp,
                volume=float(signal.get("lot_size", 0.01)),
                profit=profit,
                profit_pips=round(pips, 1),
                profit_pct=_mem_profit_pct,
                outcome=outcome_type,
                open_time=open_time,
                close_time=close_time,
                duration_mins=round(dur_mins, 1),
                mode=signal.get("account_mode") or _poll_mode,
                extra={"source": "live", "slippage_pips": _slippage_pips},
            )
            memory.record(outcome)

            # Journal: update with close data
            try:
                from engine.trade_journal import trade_journal
                from engine.account_store import current_mode
                trade_journal.log(
                    ticket=ticket,
                    symbol=signal["symbol"],
                    direction=signal["direction"],
                    volume=float(signal.get("lot_size", 0.01)),
                    entry=entry_px,
                    sl=sl,
                    tp=tp if tp else None,
                    profit=profit,
                    trading_type=signal.get("trading_mode", "day_trading"),
                    account_mode=current_mode(),
                    comment=signal.get("strategy", ""),
                    event="close",
                    close_time=close_time,
                )
            except Exception as _je:
                logger.warning(f"Journal write failed for #{ticket}: {_je}")

            trading_type = signal.get("trading_mode", "day_trading")
            stats = memory.stats(trading_type=trading_type, live_only=True, exclude_manual=True)

            # Update consecutive win/loss counter in the risk manager.
            # M-6 fix: only update when the current account mode matches the trade's mode
            # so paper losses don't trip the live circuit breaker and vice-versa.
            _drawdown_pct = 0.0
            _profit_pct   = profit
            try:
                from api.runner_loop import _risk_manager as _rm
                from engine.account_store import current_mode as _get_mode
                _cur_mode = _get_mode()
                if _rm is not None and _cur_mode == signal.get("account_mode", _cur_mode):
                    if profit > 0:
                        _rm.record_win(trading_type)
                    else:
                        _rm.record_loss(trading_type)
                    # H-3 fix: single account fetch reused for both drawdown tracking and RL reward.
                    from api.main import get_mt5_client as _gclient2
                    _c2 = _gclient2()
                    if _c2 and _c2.is_connected():
                        _acct = await asyncio.to_thread(_c2.get_account_info)
                        if _acct and _acct.get("balance"):
                            _bal = _acct["balance"]
                            _rm.update_balance(_bal)
                            # Compute current daily drawdown % for RL state
                            if _rm._day_start_balance and _rm._day_start_balance > 0:
                                _drawdown_pct = max(
                                    0.0,
                                    (_rm._day_start_balance - _bal) / _rm._day_start_balance * 100.0,
                                )
                            # Normalise profit → % of balance (scale-invariant RL reward)
                            if _bal > 0:
                                _profit_pct = profit / _bal * 100.0
            except Exception:
                pass

            rl_manager.on_trade_closed(
                trading_type=trading_type,
                profit_pct=_profit_pct,
                win_rate=stats.get("win_rate", 0.5),
                avg_conf=stats.get("avg_conf", 0.5),
                drawdown_pct=_drawdown_pct,
                vol_pct=abs(entry_px - sl) / max(abs(entry_px), 1e-8) * 100 if entry_px and sl else 0.0,
            )
            logger.info(
                f"Outcome recorded: #{ticket} {signal['symbol']} {outcome_type} "
                f"profit={profit:+.2f} ({_profit_pct:+.4f}%) pips={pips:+.1f}"
            )

            # ── Auto LSTM retrain ─────────────────────────────────────────────
            # Trigger background retrain once we have enough trades (every 20th).
            # Dedup guard: if another poll for the same symbol×mode already fired a
            # retrain within the last 2 minutes, skip — avoids double OHLCV fetches
            # that can occur when multiple positions close in the same batch before
            # predictor.is_training() is set.
            try:
                from ai.predictor import predictor, TRADING_TYPE_TF
                from ai.trade_memory import memory as _mem
                _sym   = signal["symbol"]
                _type  = trading_type
                _key   = f"{_sym}_{_type}"
                _now_ts = time.monotonic()
                if (_now_ts - _retrain_last_triggered.get(_key, 0.0)) < _RETRAIN_DEDUP_SECS:
                    raise Exception(f"retrain dedup cooldown active for {_key}")
                _total = len([
                    o for o in _mem.recent(n=500, live_only=True)
                    if o.get("symbol") == _sym and o.get("trading_type") == _type
                ])
                # Trigger 1: every 20th closed trade (baseline)
                _prev_total = _total - 1   # this trade just closed
                _retrain = (
                    not predictor.is_training(_sym, _type) and
                    _total > 0 and _total % 20 == 0 and _prev_total % 20 != 0
                )
                _retrain_reason = "20-trade window" if _retrain else ""

                # Trigger 2: model is stale (> 7 days since last training, ≥ 10 trades)
                if not _retrain and _total >= 10 and not predictor.is_training(_sym, _type):
                    _meta = predictor._metadata.get(_key, {})
                    _trained_at_iso = _meta.get("trained_at")
                    if _trained_at_iso:
                        try:
                            _age_h = (
                                datetime.now(tz=timezone.utc) -
                                datetime.fromisoformat(_trained_at_iso)
                            ).total_seconds() / 3600
                            if _age_h > 7 * 24:
                                _retrain = True
                                _retrain_reason = f"stale model ({_age_h:.0f}h old)"
                        except Exception:
                            pass

                # Trigger 3: 5 consecutive losses (regime change indicator)
                if not _retrain and not predictor.is_training(_sym, _type):
                    _recent_5 = [
                        o for o in _mem.recent(n=30, live_only=True)
                        if o.get("symbol") == _sym and o.get("trading_type") == _type
                    ][-5:]
                    if len(_recent_5) == 5 and all(o.get("profit", 0) < 0 for o in _recent_5):
                        _retrain = True
                        _retrain_reason = "5 consecutive losses"

                if _retrain:
                    _tf_str = TRADING_TYPE_TF.get(_type, "H1")
                    from api.main import get_mt5_client
                    _client = get_mt5_client()
                    if _client and _client.is_connected():
                        _df = await asyncio.to_thread(
                            _client.get_ohlcv, _sym, _tf_str,
                            _RETRAIN_BARS.get(_type, 5_000)
                        )
                        if _df is not None and not _df.empty:
                            _retrain_last_triggered[_key] = time.monotonic()   # stamp dedup timer
                            predictor.train_async(_sym, _df, _type)
                            logger.info(f"Auto LSTM retrain triggered [{_retrain_reason}]: {_key} ({_total} trades)")
            except Exception as _exc:
                logger.debug(f"Auto LSTM retrain skipped: {_exc}")

            # ── Auto param optimizer (recent data) ───────────────────────────────
            # Trigger when enough trades exist and win_rate suggests re-optimization.
            # Uses recent 3-6 month data only (not full 2-year) for FAST adaptation
            # to current market regime — avoids expensive backtest when parameters
            # are underperforming due to market shift.
            try:
                from ai.param_optimizer import optimizer as _opt
                _strat = signal.get("strategy", "")
                _sym   = signal["symbol"]
                if _strat and _opt.should_reoptimize(_strat, _sym):
                    _tf_str2 = TRADING_TYPE_TF.get(trading_type, "H1")
                    from api.main import get_mt5_client as _gclient
                    _client2 = _gclient()
                    if _client2 and _client2.is_connected():
                        _df2 = await asyncio.to_thread(
                            _client2.get_ohlcv, _sym, _tf_str2,
                            _AUTO_OPT_BARS.get(trading_type, 150)  # Use recent data, not 2-year
                        )
                        if _df2 is not None and not _df2.empty:
                            _opt.optimize_async(_strat, _sym, _df2, trading_type)
                            logger.info(f"Auto param optimizer triggered (recent {_AUTO_OPT_BARS.get(trading_type, 150)} bars): {_strat}/{_sym}")
            except Exception as _exc2:
                logger.debug(f"Auto param optimizer skipped: {_exc2}")

            return

        except Exception as exc:
            logger.warning(f"_poll_outcome error for #{ticket}: {exc}")
            await asyncio.sleep(60)


# Application-level singleton — import this everywhere
bus = SignalBus()
