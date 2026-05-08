from __future__ import annotations

"""
EVOTRADE AI Signal Bus — compatibility-safe architecture rewrite.

This keeps the old public API expected by routes/runner:

    from api.signal_bus import bus

    await bus.add_signal(signal)
    await bus.execute_signal(signal_id)
    bus.queue
    bus.archive
    bus.init(client, order_manager)
    await bus.restore_pending_swing_signals()

Heavy responsibilities are delegated to:
- engine.trade_execution.TradeExecutionService
- engine.trade_outcome.poll_trade_outcome
- engine.trade_recovery.recover_unclosed_trades
"""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from engine.notification_manager import notification_manager
from engine.signal_journal import signal_journal
from engine.trade_execution import TradeExecutionService
from engine.trade_outcome import poll_trade_outcome
from engine.trade_recovery import recover_unclosed_trades
from engine.trade_identity import account_login_from, account_mode_from
from engine.utils.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

_event_loop: Optional[asyncio.AbstractEventLoop] = None

CONFIG_DIR = Path(__file__).parent.parent / "config"
_PENDING_SIGNALS_FILE = Path(__file__).parent.parent / "data" / "pending_signals.json"

_SIGNAL_TTL_SECONDS = 3600
_TERMINAL_STATUSES = frozenset({"executed", "rejected", "failed", "expired", "blocked"})

_DEFAULT_EXPIRY: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

_app_cfg_cache_bus: dict[str, Any] = {}
_app_cfg_loaded_at_bus: float = 0.0
_APP_CFG_TTL_BUS = 5.0

_active_poll_tasks: set[asyncio.Task] = set()


def _set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _event_loop
    _event_loop = loop


def _get_bus_app_cfg() -> dict[str, Any]:
    global _app_cfg_cache_bus, _app_cfg_loaded_at_bus

    now = time.monotonic()
    if now - _app_cfg_loaded_at_bus < _APP_CFG_TTL_BUS:
        return _app_cfg_cache_bus

    try:
        _app_cfg_cache_bus = json.loads(
            (CONFIG_DIR / "app.json").read_text(encoding="utf-8-sig")
        )
    except Exception as exc:
        logger.debug("SignalBus app config reload failed, using stale cache: %s", exc)

    _app_cfg_loaded_at_bus = now
    return _app_cfg_cache_bus


class SignalBus:
    """
    Queue coordinator + compatibility layer.

    Execution, lifecycle polling, recovery, and learning are now delegated to
    dedicated engine modules.
    """

    def __init__(self) -> None:
        self.queue: dict[str, dict[str, Any]] = {}
        self.archive: deque[dict[str, Any]] = deque(maxlen=500)

        self._client = None
        self._order_manager = None
        self._execution_service: TradeExecutionService | None = None

        self._pending_keys: set[tuple[Any, ...]] = set()
        self._active_tasks: set[asyncio.Task] = set()

    def init(self, client, order_manager) -> None:
        """
        Existing startup hook used by api.main.
        """
        self._client = client
        self._order_manager = order_manager
        self._execution_service = TradeExecutionService(client, order_manager)

    def bind(self, *, client, order_manager) -> None:
        """
        Alias for newer architecture naming.
        """
        self.init(client, order_manager)

    # ── identity / signal helpers ──────────────────────────────────────────

    @staticmethod
    def _signal_score(signal: dict[str, Any]) -> float:
        try:
            return float(signal.get("score") or signal.get("confidence") or 0.0)
        except Exception:
            return 0.0

    @staticmethod
    def _same_symbol_mode(a: dict[str, Any], b: dict[str, Any]) -> bool:
        return (
            normalize_symbol(a.get("symbol", "")) == normalize_symbol(b.get("symbol", ""))
            and a.get("trading_mode", "") == b.get("trading_mode", "")
        )

    @staticmethod
    def _pending_key(signal: dict[str, Any]) -> tuple[Any, ...]:
        return (
            signal.get("symbol"),
            signal.get("strategy"),
            signal.get("direction"),
            str(signal.get("trading_mode") or ""),
            account_mode_from(signal),
            account_login_from(signal),
        )

    def _release_pending_key(self, signal: dict[str, Any]) -> None:
        self._pending_keys.discard(self._pending_key(signal))

    @staticmethod
    def _account_login_from_signal(signal: dict[str, Any]) -> int:
        return account_login_from(signal)

    @staticmethod
    def _execution_mode_from_signal(signal: dict[str, Any]) -> str:
        return account_mode_from(signal)

    def _signal_id(self, signal: dict[str, Any]) -> str:
        account_login = self._account_login_from_signal(signal)
        return str(
            signal.get("signal_id")
            or signal.get("id")
            or f"{account_login}:{signal.get('symbol')}:{signal.get('strategy')}:{signal.get('direction')}"
        )

    def _record_signal_journal(
        self,
        signal: dict[str, Any],
        *,
        status: str,
        decision: str,
        reason: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> None:
        try:
            if status:
                signal["status"] = status
            if reason:
                signal["rejection_reason"] = reason

            account_login = self._account_login_from_signal(signal)
            account_mode = self._execution_mode_from_signal(signal)

            signal_journal.record({
                "signal_id": self._signal_id(signal),
                "symbol_raw": signal.get("symbol"),
                "symbol_normalized": normalize_symbol(signal.get("symbol", "")),
                "strategy": signal.get("strategy"),
                "trading_type": signal.get("trading_mode", ""),
                "mode": signal.get("trading_mode", ""),
                "execution_mode": account_mode,
                "direction": signal.get("direction"),
                "confidence": float(signal.get("confidence") or 0.0),
                "score": float(signal.get("score") or 0.0),
                "entry": signal.get("entry") or signal.get("entry_price"),
                "sl": signal.get("sl"),
                "tp": signal.get("tp"),
                "timeframe": signal.get("timeframe") or signal.get("tf"),
                "regime": signal.get("regime"),
                "rl_state": signal.get("rl_state"),
                "status": status,
                "decision": decision,
                "reason": signal.get("rejection_reason"),
                "account_login": account_login,
                "account_type": signal.get("account_type", account_mode),
                "user_id": signal.get("user_id", "default"),
                "filters": filters or {},
            })
        except Exception as exc:
            logger.debug("SignalBus: signal_journal write skipped: %s", exc)

    def _archive_signal(self, signal: dict[str, Any], *, status: str, reason: str) -> None:
        signal["status"] = status
        signal["rejection_reason"] = reason
        self._release_pending_key(signal)
        self.archive.append(dict(signal))

    def _notify_signal_generated(
        self,
        signal: dict[str, Any],
        mode: str,
        *,
        swing_delay: int | None = None,
    ) -> None:
        if swing_delay is not None:
            title = "New Swing Signal"
            message = (
                f"{str(signal.get('direction', '')).upper()} {signal.get('symbol')} "
                f"via {signal.get('strategy')} "
                f"(conf: {float(signal.get('confidence') or 0):.0%}) "
                f"— auto-executes in {swing_delay}s"
            )
        else:
            title = f"New {mode.replace('_', ' ').title()} Signal"
            message = (
                f"{str(signal.get('direction', '')).upper()} {signal.get('symbol')} "
                f"via {signal.get('strategy')} "
                f"(conf: {float(signal.get('confidence') or 0):.0%})"
            )

        notification_manager.add(
            type="signal_generated",
            title=title,
            message=message,
            severity="info",
            metadata={
                "signal_id": signal.get("id"),
                "symbol": signal.get("symbol"),
                "trading_mode": mode,
            },
        )

    # ── public route API ───────────────────────────────────────────────────

    async def add_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        """
        Existing runner/routes entrypoint.
        """
        from api.websocket.feed import broadcast_signal

        mode = str(signal.get("trading_mode") or "")

        if not signal.get("id"):
            signal["id"] = self._signal_id(signal)

        if not signal.get("created_at"):
            signal["created_at"] = datetime.now(timezone.utc).isoformat()

        dedup_key = self._pending_key(signal)
        new_score = self._signal_score(signal)
        conflict_margin = 0.08

        if dedup_key in self._pending_keys:
            signal["rejection_reason"] = "Duplicate signal already pending/executing"
            self._record_signal_journal(
                signal,
                status="blocked",
                decision="blocked_by_filter",
                filters={"filter": "duplicate_fast_set"},
            )
            return signal

        for existing in self.queue.values():
            if existing.get("status") not in ("pending", "executing"):
                continue

            if self._pending_key(existing) == dedup_key:
                signal["rejection_reason"] = "Duplicate signal already pending/executing"
                self._record_signal_journal(
                    signal,
                    status="blocked",
                    decision="blocked_by_filter",
                    filters={
                        "filter": "duplicate_existing_signal",
                        "existing_status": existing.get("status"),
                    },
                )
                return existing

        # Open-position guard by symbol + trading mode.
        try:
            if self._client is not None:
                open_positions = self._client.get_open_positions()
                sym = signal.get("symbol", "")
                direction = str(signal.get("direction", "")).upper()
                mode_prefix = {
                    "scalping": "scalp",
                    "day_trading": "day",
                    "swing": "swing",
                }.get(mode, mode)

                try:
                    from engine.order_manager import BOT_MAGIC
                    bot_magic = BOT_MAGIC
                except Exception:
                    bot_magic = None

                for pos in open_positions or []:
                    pos_comment = str(pos.get("comment", ""))
                    if (
                        (bot_magic is None or pos.get("magic") == bot_magic)
                        and normalize_symbol(pos.get("symbol", "")) == normalize_symbol(sym)
                        and pos_comment.startswith(mode_prefix)
                    ):
                        raw_type = pos.get("type", "")
                        if isinstance(raw_type, str):
                            open_dir = raw_type.upper()
                        else:
                            open_dir = "BUY" if int(raw_type) == 0 else "SELL"

                        if open_dir == direction:
                            reason = f"Open {direction} {mode} position already exists for {sym}"
                            filter_name = "open_position_exists"
                        else:
                            reason = (
                                f"Opposite-direction conflict: open {open_dir} {mode} "
                                f"position already exists for {sym}; new {direction} rejected"
                            )
                            filter_name = "position_conflict"

                        signal["rejection_reason"] = reason
                        self._record_signal_journal(
                            signal,
                            status="blocked",
                            decision="blocked_by_risk",
                            filters={
                                "filter": filter_name,
                                "open_direction": open_dir,
                                "new_direction": direction,
                                "symbol": sym,
                                "trading_type": mode,
                            },
                        )
                        return signal
        except Exception:
            pass

        # Queue arbitration: same symbol + same trading mode.
        for existing in list(self.queue.values()):
            if existing.get("status") not in ("pending", "executing"):
                continue

            if not self._same_symbol_mode(existing, signal):
                continue

            existing_dir = str(existing.get("direction", "")).upper()
            new_dir = str(signal.get("direction", "")).upper()
            existing_score = self._signal_score(existing)

            if existing_dir == new_dir:
                if new_score > existing_score:
                    existing["status"] = "rejected"
                    existing["rejection_reason"] = (
                        f"Replaced by higher-score {new_dir} signal "
                        f"({new_score:.2f} > {existing_score:.2f})"
                    )
                    self._record_signal_journal(
                        existing,
                        status="rejected",
                        decision="replaced_by_higher_score",
                        filters={
                            "filter": "same_direction_arbitration",
                            "new_score": new_score,
                            "existing_score": existing_score,
                            "winner_direction": new_dir,
                        },
                    )
                    self._release_pending_key(existing)
                    self.archive.append(dict(existing))
                    continue

                signal["rejection_reason"] = (
                    f"Lower-score same-direction signal rejected "
                    f"({new_score:.2f} <= {existing_score:.2f})"
                )
                self._record_signal_journal(
                    signal,
                    status="blocked",
                    decision="blocked_by_filter",
                    filters={
                        "filter": "same_direction_arbitration",
                        "new_score": new_score,
                        "existing_score": existing_score,
                    },
                )
                return signal

            score_gap = abs(new_score - existing_score)
            if score_gap < conflict_margin:
                reason = (
                    f"Direction conflict rejected: {new_dir} score {new_score:.2f} "
                    f"vs {existing_dir} score {existing_score:.2f}; gap too small"
                )
                signal["rejection_reason"] = reason
                existing["status"] = "rejected"
                existing["rejection_reason"] = (
                    f"Direction conflict rejected against {new_dir}; gap too small"
                )

                filters = {
                    "filter": "direction_conflict_gap_too_small",
                    "new_direction": new_dir,
                    "existing_direction": existing_dir,
                    "new_score": new_score,
                    "existing_score": existing_score,
                    "score_gap": score_gap,
                    "required_gap": conflict_margin,
                }
                self._record_signal_journal(
                    signal,
                    status="blocked",
                    decision="blocked_by_filter",
                    filters=filters,
                )
                self._record_signal_journal(
                    existing,
                    status="rejected",
                    decision="direction_conflict",
                    filters=filters,
                )
                self._release_pending_key(existing)
                self.archive.append(dict(existing))
                return signal

            if new_score > existing_score:
                existing["status"] = "rejected"
                existing["rejection_reason"] = (
                    f"Lost direction arbitration to {new_dir} "
                    f"({new_score:.2f} > {existing_score:.2f})"
                )
                self._record_signal_journal(
                    existing,
                    status="rejected",
                    decision="lost_direction_arbitration",
                    filters={
                        "filter": "direction_arbitration",
                        "winner_direction": new_dir,
                        "loser_direction": existing_dir,
                        "new_score": new_score,
                        "existing_score": existing_score,
                    },
                )
                self._release_pending_key(existing)
                self.archive.append(dict(existing))
                continue

            signal["rejection_reason"] = (
                f"Lost direction arbitration to existing {existing_dir} "
                f"({existing_score:.2f} > {new_score:.2f})"
            )
            self._record_signal_journal(
                signal,
                status="blocked",
                decision="blocked_by_filter",
                filters={
                    "filter": "direction_arbitration",
                    "winner_direction": existing_dir,
                    "loser_direction": new_dir,
                    "new_score": new_score,
                    "existing_score": existing_score,
                },
            )
            return signal

        exec_mode = self._get_exec_mode(mode)

        if exec_mode == "auto" and self._execution_service is not None:
            # RL confidence gate before queueing.
            conf = float(signal.get("confidence") or 0.0)
            try:
                from ai.rl_agent import rl_manager
                is_tradeable = rl_manager.should_take_signal(
                    signal.get("strategy") or None,
                    mode,
                    conf,
                )
            except Exception:
                is_tradeable = conf >= 0.50

            if not is_tradeable:
                signal["rejection_reason"] = f"Confidence {conf:.0%} below RL threshold for {mode}"
                self._record_signal_journal(
                    signal,
                    status="blocked",
                    decision="blocked_by_rl",
                    filters={
                        "filter": "rl_confidence_threshold",
                        "confidence": conf,
                        "trading_type": mode,
                    },
                )
                return signal

            try:
                conf_filter_on = bool(
                    _get_bus_app_cfg().get("ai", {}).get("confidence_filter_enabled", False)
                )
            except Exception:
                conf_filter_on = False

            if conf_filter_on:
                try:
                    threshold = float(
                        _get_bus_app_cfg().get("ai", {}).get("confidence_threshold", 60)
                    ) / 100.0
                except Exception:
                    threshold = 0.60

                if conf < threshold:
                    signal["rejection_reason"] = (
                        f"Confidence {conf:.0%} below floor {threshold:.0%}"
                    )
                    self._record_signal_journal(
                        signal,
                        status="blocked",
                        decision="blocked_by_filter",
                        filters={
                            "filter": "confidence_floor",
                            "confidence": conf,
                            "trading_type": mode,
                        },
                    )
                    return signal

            if mode == "swing":
                try:
                    review_secs = int(
                        _get_bus_app_cfg().get("ai", {}).get("swing_auto_review_seconds", 300)
                    )
                except Exception:
                    review_secs = 300

                signal["status"] = "pending"
                signal["expires_at"] = (
                    datetime.now(timezone.utc) + timedelta(seconds=review_secs)
                ).isoformat()
                signal["auto_execute_at"] = signal["expires_at"]

                self.queue[signal["id"]] = signal
                self._pending_keys.add(dedup_key)
                self.purge_stale()
                self._save_pending_swing_signals()

                asyncio.create_task(broadcast_signal(dict(signal)))
                self._notify_signal_generated(signal, mode, swing_delay=review_secs)

                task = asyncio.create_task(self._swing_auto_execute(signal, review_secs))
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)
                return signal

            signal["status"] = "executing"
            self.queue[signal["id"]] = signal
            self._pending_keys.add(dedup_key)
            self.purge_stale()

            asyncio.create_task(broadcast_signal(dict(signal)))
            self._notify_signal_generated(signal, mode)

            task = asyncio.create_task(self._execute_async(signal))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)
            return signal

        # Manual mode.
        try:
            expiry_cfg = _get_bus_app_cfg().get("signal_expiry_seconds", {})
        except Exception:
            expiry_cfg = {}

        tf = signal.get("timeframe", "")
        expiry_seconds = expiry_cfg.get(tf) or _DEFAULT_EXPIRY.get(tf, 1800)

        signal["expires_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds)
        ).isoformat()
        signal["status"] = "pending"

        self.queue[signal["id"]] = signal
        self._pending_keys.add(dedup_key)
        self.purge_stale()

        asyncio.create_task(broadcast_signal(dict(signal)))
        self._notify_signal_generated(signal, mode)

        return signal

    async def execute_signal(self, signal_id: str) -> dict[str, Any]:
        """
        Manual approval endpoint compatibility.
        """
        signal = self.queue.get(signal_id)
        if signal is None:
            raise KeyError(signal_id)

        expires_at = signal.get("expires_at")
        if expires_at and not signal.get("auto_execute_at"):
            try:
                exp_dt = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
                if datetime.now(timezone.utc) > exp_dt:
                    signal["status"] = "expired"
                    signal["rejection_reason"] = "Signal expired before manual approval"
                    self._record_signal_journal(
                        signal,
                        status="expired",
                        decision="shadow_only",
                        filters={"filter": "manual_approval_expired"},
                    )
                    raise RuntimeError(
                        f"Signal {signal_id} expired at {expires_at} — cannot execute"
                    )
            except RuntimeError:
                raise
            except Exception:
                pass

        signal["status"] = "executing"

        task = asyncio.create_task(self._execute_async(signal))
        self._active_tasks.add(task)
        task.add_done_callback(self._active_tasks.discard)

        return signal

    async def _swing_auto_execute(self, signal: dict[str, Any], delay: float) -> None:
        from api.websocket.feed import broadcast_signal

        await asyncio.sleep(delay)

        if signal.get("status") != "pending":
            self._release_pending_key(signal)
            return

        signal["status"] = "executing"
        await broadcast_signal({**signal, "type": "signal_update"})

        try:
            from api.runner_loop import _risk_manager
            if _risk_manager is not None:
                ok, msg = _risk_manager.is_trading_allowed(signal.get("trading_mode", "swing"))
                if not ok:
                    signal["status"] = "blocked"
                    signal["rejection_reason"] = msg
                    self._release_pending_key(signal)
                    self._record_signal_journal(
                        signal,
                        status="blocked",
                        decision="blocked_by_risk",
                        filters={"filter": "swing_auto_circuit_breaker"},
                    )
                    await broadcast_signal({**signal, "type": "signal_update"})
                    self._save_pending_swing_signals()
                    return
        except Exception:
            pass

        await self._execute_async(signal)
        self._save_pending_swing_signals()

    async def _execute_async(self, signal: dict[str, Any]) -> None:
        from api.websocket.feed import broadcast_signal

        try:
            if self._execution_service is None:
                raise RuntimeError("SignalBus not initialized — execution service missing")

            result = await asyncio.to_thread(
                self._execution_service.execute,
                signal,
                schedule_poll=self._schedule_poll_threadsafe,
            )

            if result.success:
                signal["status"] = "executed"
            elif signal.get("status") not in _TERMINAL_STATUSES:
                signal["status"] = "failed"

            if result.error and not signal.get("rejection_reason"):
                signal["rejection_reason"] = result.error

            signal["actioned_at"] = datetime.now(timezone.utc).isoformat()

        except Exception as exc:
            logger.exception("SignalBus execution error: %s", exc)
            signal["status"] = "failed"
            signal["error"] = str(exc)
            signal.setdefault("rejection_reason", str(exc))
            self._record_signal_journal(
                signal,
                status="failed",
                decision="execution_error",
                filters={"filter": "execute_async_exception"},
            )

        self._release_pending_key(signal)
        await broadcast_signal({**signal, "type": "signal_update"})

        if signal.get("auto_execute_at"):
            self._save_pending_swing_signals()

    def _schedule_poll_threadsafe(self, ticket: int, signal: dict[str, Any]) -> None:
        loop = _event_loop
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.schedule_poll(ticket=ticket, signal=signal),
                loop,
            )
            return

        # If already on running loop.
        try:
            asyncio.get_running_loop().create_task(
                self.schedule_poll(ticket=ticket, signal=signal)
            )
        except RuntimeError:
            logger.warning("Could not schedule poll for ticket #%s — no running event loop", ticket)

    async def schedule_poll(self, ticket: int, signal: dict[str, Any], client=None) -> None:
        """
        Public async poll scheduler used by recovery.
        """
        active_client = client or self._client
        if active_client is None:
            logger.error("Cannot schedule outcome poll — MT5 client missing")
            return

        task = asyncio.create_task(
            poll_trade_outcome(ticket=ticket, signal=signal, client=active_client)
        )
        _active_poll_tasks.add(task)

        def _cleanup(done_task: asyncio.Task) -> None:
            _active_poll_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Outcome poll task failed")

        task.add_done_callback(_cleanup)

    # ── recovery compatibility ──────────────────────────────────────────────

    async def recover_unclosed_trades(self, client=None) -> None:
        await recover_unclosed_trades(
            client or self._client,
            poll_callback=self.schedule_poll,
        )

    async def restore_pending_swing_signals(self) -> None:
        """
        Existing startup hook.
        """
        if not _PENDING_SIGNALS_FILE.exists():
            return

        try:
            saved: list[dict[str, Any]] = json.loads(
                _PENDING_SIGNALS_FILE.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("SignalBus: could not read pending signals file: %s", exc)
            return

        if not saved:
            return

        from api.websocket.feed import broadcast_signal

        now = datetime.now(timezone.utc)
        restored = 0

        for sig in saved:
            auto_at_str = sig.get("auto_execute_at")
            if not auto_at_str or sig.get("status") != "pending":
                continue

            try:
                auto_at = datetime.fromisoformat(str(auto_at_str).replace("Z", "+00:00"))
            except Exception:
                continue

            elapsed = (now - auto_at).total_seconds()
            if elapsed > 3600:
                continue

            if not sig.get("id"):
                sig["id"] = self._signal_id(sig)

            self.queue[sig["id"]] = sig
            self._pending_keys.add(self._pending_key(sig))

            delay = max(0.0, (auto_at - now).total_seconds())
            task = asyncio.create_task(self._swing_auto_execute(sig, delay))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

            asyncio.create_task(broadcast_signal(dict(sig)))
            restored += 1

        if restored:
            logger.info("SignalBus: %s pending swing signal(s) restored from disk", restored)

        self._save_pending_swing_signals()

    # ── queue maintenance ───────────────────────────────────────────────────

    def purge_stale(self) -> int:
        now = datetime.now(timezone.utc)
        to_delete: list[str] = []

        for sid, sig in list(self.queue.items()):
            if sig.get("status") in _TERMINAL_STATUSES:
                try:
                    created = datetime.fromisoformat(
                        str(sig.get("created_at", "")).replace("Z", "+00:00")
                    )
                    if (now - created).total_seconds() > _SIGNAL_TTL_SECONDS:
                        to_delete.append(sid)
                except Exception:
                    to_delete.append(sid)
                continue

            if sig.get("auto_execute_at"):
                continue

            if sig.get("status") == "pending" and sig.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(str(sig["expires_at"]).replace("Z", "+00:00"))
                    if now >= exp:
                        sig["status"] = "expired"
                        sig["rejection_reason"] = "Signal expired — market conditions may have changed"
                        self._record_signal_journal(
                            sig,
                            status="expired",
                            decision="shadow_only",
                            filters={
                                "filter": "signal_expiry",
                                "expired_at": now.isoformat(),
                            },
                        )
                        self._release_pending_key(sig)
                except Exception:
                    pass

        for sid in to_delete:
            sig = self.queue.pop(sid, None)
            if sig:
                self.archive.append(dict(sig))
                self._release_pending_key(sig)

        if to_delete:
            self._save_pending_swing_signals()

        return len(to_delete)

    def _save_pending_swing_signals(self) -> None:
        try:
            pending = [
                dict(sig)
                for sig in self.queue.values()
                if sig.get("auto_execute_at") and sig.get("status") == "pending"
            ]

            _PENDING_SIGNALS_FILE.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(pending, indent=2)

            fd, tmp = tempfile.mkstemp(
                dir=str(_PENDING_SIGNALS_FILE.parent),
                suffix=".tmp",
            )

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(serialized)
                os.replace(tmp, _PENDING_SIGNALS_FILE)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        except Exception as exc:
            logger.debug("SignalBus: could not save pending swing signals: %s", exc)

    @staticmethod
    def _get_exec_mode(trading_type: str) -> str:
        try:
            cfg = _get_bus_app_cfg()
            return cfg.get("execution_mode", {}).get(trading_type, "manual")
        except Exception:
            return "manual"

    @staticmethod
    def _is_ea_enabled() -> bool:
        try:
            return bool(_get_bus_app_cfg().get("ea_enabled", False))
        except Exception:
            return False


# Compatibility export expected by existing routes.
bus = SignalBus()
signal_bus = bus
