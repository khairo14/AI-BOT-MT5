from __future__ import annotations

"""
Trade execution service for EVOTRADE AI.

Responsibilities:
- execute approved/auto signals through MT5 OrderManager
- run final execution-time guards
- write trade open journal records
- initialize persistent trade lifecycle state
- return execution result for SignalBus queue updates

This module intentionally does NOT:
- poll trade outcomes
- process TP1/BE/trailing lifecycle after open
- write TradeMemory
- feed RL on close
- recover offline trades
"""

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from engine.notification_manager import notification_manager
from engine.order_manager import OrderRequest
from engine.trade_identity import current_trade_identity, enrich_with_trade_identity
from engine.trade_lifecycle import mark_trade_open
from engine.utils.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    success: bool
    signal: dict[str, Any]
    ticket: Optional[int] = None
    error: Optional[str] = None


def _mode_prefix(trading_mode: str) -> str:
    return {
        "scalping": "scalp",
        "day_trading": "day",
        "swing": "swing",
    }.get(trading_mode, "bot")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _record_signal_journal_safe(
    signal: dict[str, Any],
    *,
    status: str,
    decision: str,
    filters: dict | None = None,
) -> None:
    """
    Late import to avoid circular dependency with api.signal_bus.

    This is a temporary bridge. Later, signal journaling can be moved into a
    dedicated signal service.
    """
    try:
        from engine.signal_journal import signal_journal

        identity = current_trade_identity(signal)
        signal_journal.record({
            "signal_id": signal.get("signal_id") or signal.get("id"),
            "symbol_raw": signal.get("symbol"),
            "symbol_normalized": normalize_symbol(signal.get("symbol", "")),
            "strategy": signal.get("strategy"),
            "trading_type": signal.get("trading_mode", ""),
            "mode": signal.get("trading_mode", ""),
            "execution_mode": identity.account_mode,
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
            "account_login": identity.account_login,
            "account_type": identity.account_type,
            "user_id": identity.user_id,
            "filters": filters or {},
        })
    except Exception as exc:
        logger.debug("Signal journal write skipped: %s", exc)


def _current_risk_manager():
    try:
        from api.runner_loop import _risk_manager

        return _risk_manager
    except Exception:
        return None


def _get_bus_app_cfg() -> dict:
    """
    Read app.json through signal_bus cache when available.

    Keeps behavior aligned with the old signal_bus implementation without
    duplicating config cache logic here.
    """
    try:
        from api.signal_bus import _get_bus_app_cfg as _cfg

        return _cfg()
    except Exception:
        try:
            import json
            from pathlib import Path

            return json.loads((Path(__file__).parent.parent / "config" / "app.json").read_text(encoding="utf-8-sig"))
        except Exception:
            return {}


def _block_signal(
    signal: dict[str, Any],
    *,
    reason: str,
    decision: str,
    filter_name: str,
    filters: dict | None = None,
) -> ExecutionResult:
    signal["rejection_reason"] = reason
    signal["status"] = "blocked"

    payload_filters = {"filter": filter_name}
    if filters:
        payload_filters.update(filters)

    _record_signal_journal_safe(
        signal,
        status="blocked",
        decision=decision,
        filters=payload_filters,
    )

    return ExecutionResult(False, signal, error=reason)


class TradeExecutionService:
    """
    Encapsulates order placement and open-state side effects.
    """

    def __init__(self, client, order_manager):
        self.client = client
        self.order_manager = order_manager

    def execute(
        self,
        signal: dict[str, Any],
        *,
        schedule_poll: Callable[[int, dict[str, Any]], None] | None = None,
    ) -> ExecutionResult:
        """
        Execute one signal synchronously.

        schedule_poll receives (ticket, signal) after successful open.
        """
        if self.order_manager is None or self.client is None:
            err = "TradeExecutionService not initialised — order manager or MT5 client missing"
            signal["rejection_reason"] = err
            _record_signal_journal_safe(
                signal,
                status="failed",
                decision="execution_unavailable",
                filters={"filter": "trade_execution_not_initialised"},
            )
            logger.error(err)
            return ExecutionResult(False, signal, error=err)

        trading_mode = signal.get("trading_mode", "day_trading")
        mode_prefix = _mode_prefix(trading_mode)

        # Execution-time risk checks.
        risk_manager = _current_risk_manager()

        if risk_manager is not None:
            try:
                allowed, reason = risk_manager.is_trading_allowed(trading_mode)
                if not allowed:
                    notification_manager.add(
                        type="circuit_breaker",
                        title="Circuit Breaker Activated",
                        message=f"{trading_mode.replace('_', ' ').title()}: {reason}",
                        severity="warning",
                        metadata={"trading_mode": trading_mode, "reason": reason},
                    )

                    return _block_signal(
                        signal,
                        reason=reason,
                        decision="blocked_by_risk",
                        filter_name="circuit_breaker",
                    )

                strategy_name = signal.get("strategy")
                if strategy_name:
                    strat_ok, strat_reason = risk_manager.is_strategy_allowed(strategy_name)
                    if not strat_ok:
                        notification_manager.add(
                            type="circuit_breaker",
                            title="Strategy Circuit Breaker",
                            message=strat_reason,
                            severity="warning",
                            metadata={"strategy": strategy_name, "reason": strat_reason},
                        )

                        return _block_signal(
                            signal,
                            reason=strat_reason,
                            decision="blocked_by_risk",
                            filter_name="strategy_circuit_breaker",
                            filters={"strategy": strategy_name},
                        )

                open_positions = self.client.get_open_positions()
                concurrent_ok, concurrent_reason = risk_manager.check_concurrent_limit(
                    trading_mode,
                    open_positions,
                    symbol=signal.get("symbol"),
                )
                if not concurrent_ok:
                    return _block_signal(
                        signal,
                        reason=concurrent_reason,
                        decision="blocked_by_risk",
                        filter_name="concurrent_limit",
                    )

            except Exception as exc:
                logger.debug("Execution risk checks skipped: %s", exc)

        # EA fast path for scalping.
        ea_used = False
        if trading_mode == "scalping" and self._is_ea_enabled():
            try:
                from engine.ea_bridge import ea_bridge

                if ea_bridge.is_active():
                    ea_used = ea_bridge.submit_and_wait(signal, timeout=5.0)

                    if ea_used:
                        logger.info(
                            "Signal executed via EA: %s %s lot=%s ticket=%s",
                            signal.get("symbol"),
                            signal.get("direction"),
                            signal.get("lot_size"),
                            signal.get("ticket"),
                        )
                    else:
                        logger.info(
                            "EA execution failed/timeout for %s — falling back to Python order",
                            signal.get("symbol"),
                        )
                else:
                    logger.debug("EABridge: EA not active — using Python execution")
            except Exception as exc:
                logger.warning("EABridge error during dispatch: %s — falling back", exc)

        if ea_used:
            ticket = signal.get("ticket")
            fill_price = signal.get("fill_price")
            volume = _safe_float(signal.get("lot_size"), 0.01)

            self._write_open_journal_and_state(
                signal,
                ticket=int(ticket or 0),
                volume=volume,
                fill_price=_safe_float(fill_price),
                slippage=None,
                execution_time_ms=None,
                spread_pips=None,
            )

            if ticket is not None and schedule_poll is not None:
                schedule_poll(int(ticket), signal)

            return ExecutionResult(True, signal, ticket=int(ticket or 0))

        # Standard Python execution.
        entry_raw = signal.get("entry_price") or signal.get("entry")

        stale = self._check_stale_entry(signal, entry_raw)
        if stale is not None:
            return stale

        lot = self._revalidate_lot(signal, entry_raw)

        if lot <= 0:
            return _block_signal(
                signal,
                reason="Lot size is zero — SL distance is zero or invalid; signal rejected",
                decision="blocked_by_filter",
                filter_name="invalid_lot_size",
            )

        spread_block = self._check_spread_guard(signal, trading_mode)
        if spread_block is not None:
            return spread_block

        tp1_order = _safe_float(signal.get("tp"), 0.0) or None
        tp2_order = _safe_float(signal.get("tp2"), 0.0) or None

        if tp2_order and trading_mode != "scalping":
            broker_tp = tp2_order
        else:
            broker_tp = tp1_order

        req = OrderRequest(
            symbol=signal["symbol"],
            direction=str(signal["direction"]).upper(),
            volume=lot,
            sl=float(signal["sl"]),
            tp=broker_tp,
            entry_price=float(entry_raw) if entry_raw else None,
            comment=f"{mode_prefix}|{signal.get('strategy', '?')[:20]}",
        )

        result = self.order_manager.place_market_order(req)

        if not result or not result.success:
            err = result.error if result else "Order manager returned no result"
            signal["rejection_reason"] = err
            signal["status"] = "failed"
            _record_signal_journal_safe(
                signal,
                status="failed",
                decision="order_send_failed",
                filters={"filter": "order_manager_failed"},
            )
            logger.warning(
                "Signal execution failed: %s %s — %s",
                signal.get("symbol"),
                signal.get("direction"),
                err,
            )
            return ExecutionResult(False, signal, error=err)

        signal["ticket"] = result.ticket
        signal["fill_price"] = result.open_price
        signal["lot_size"] = req.volume

        logger.info(
            "Signal executed: %s %s lot=%s ticket=%s",
            signal["symbol"],
            signal["direction"],
            req.volume,
            result.ticket,
        )

        notification_manager.add(
            type="position_opened",
            title="Position Opened",
            message=(
                f"{str(signal['direction']).upper()} {signal['symbol']} "
                f"({req.volume} lots) via {signal.get('strategy', 'manual')} "
                f"— Ticket #{result.ticket}"
            ),
            severity="success",
            metadata={
                "ticket": result.ticket,
                "symbol": signal["symbol"],
                "direction": signal["direction"],
                "volume": req.volume,
                "trading_mode": signal.get("trading_mode", "day_trading"),
            },
        )

        self._write_open_journal_and_state(
            signal,
            ticket=int(result.ticket or 0),
            volume=req.volume,
            fill_price=_safe_float(result.open_price),
            slippage=result.slippage,
            execution_time_ms=result.execution_time_ms,
            spread_pips=result.spread_pips,
        )

        if result.ticket is not None and schedule_poll is not None:
            schedule_poll(int(result.ticket), signal)

        return ExecutionResult(True, signal, ticket=int(result.ticket or 0))

    def _write_open_journal_and_state(
        self,
        signal: dict[str, Any],
        *,
        ticket: int,
        volume: float,
        fill_price: float,
        slippage: float | None,
        execution_time_ms: int | None,
        spread_pips: float | None,
    ) -> None:
        identity = current_trade_identity(signal)

        enrich_with_trade_identity(signal)

        try:
            from engine.trade_journal import trade_journal

            trade_journal.log(
                ticket=ticket,
                symbol=signal["symbol"],
                direction=signal["direction"],
                volume=volume,
                entry=fill_price or 0.0,
                sl=float(signal.get("sl") or 0),
                tp=float(signal["tp"]) if signal.get("tp") else None,
                tp2=float(signal["tp2"]) if signal.get("tp2") else None,
                tp3=float(signal["tp3"]) if signal.get("tp3") else None,
                profit=None,
                trading_type=signal.get("trading_mode", "day_trading"),
                account_mode=identity.account_mode,
                account_login=identity.account_login,
                account_type=identity.account_type,
                user_id=identity.user_id,
                comment=signal.get("strategy", ""),
                strategy=signal.get("strategy", ""),
                event="open",
                confidence=float(signal.get("confidence") or 0.5),
                expected_price=signal.get("entry_price"),
                slippage=slippage,
                execution_time_ms=execution_time_ms,
                spread_pips=spread_pips,
            )
        except Exception as exc:
            logger.warning("Open journal write failed for ticket=%s: %s", ticket, exc)

        try:
            mark_trade_open(
                ticket,
                source=signal,
                payload={
                    "symbol_raw": signal["symbol"],
                    "symbol_normalized": normalize_symbol(signal["symbol"]),
                    "mode": signal.get("trading_mode", "day_trading"),
                    "strategy": signal.get("strategy", ""),
                    "direction": str(signal.get("direction", "")).upper(),
                    "entry": fill_price or 0.0,
                    "sl": float(signal.get("sl") or 0),
                    "tp1": float(signal["tp"]) if signal.get("tp") else None,
                    "tp2": float(signal["tp2"]) if signal.get("tp2") else None,
                    "volume": volume,
                    "last_event": "opened",
                },
            )
        except Exception as exc:
            logger.warning("Trade lifecycle open write failed for ticket=%s: %s", ticket, exc)

    def _check_stale_entry(
        self,
        signal: dict[str, Any],
        entry_raw: Any,
    ) -> ExecutionResult | None:
        try:
            tp_guard = _safe_float(signal.get("tp"), 0.0)
            entry_guard = _safe_float(entry_raw, 0.0)

            if not tp_guard or not entry_guard:
                return None

            price = self.client.get_current_price(signal["symbol"]) or {}
            direction = str(signal.get("direction", "")).upper()
            current = price.get("bid") if direction == "BUY" else price.get("ask")

            if current is None:
                return None

            tp_span = abs(tp_guard - entry_guard)
            if tp_span <= 0:
                return None

            if direction == "BUY":
                progress = (current - entry_guard) / tp_span
            else:
                progress = (entry_guard - current) / tp_span

            try:
                max_progress = float(
                    _get_bus_app_cfg()
                    .get("ai", {})
                    .get("max_entry_progress_to_tp", 0.80)
                )
            except Exception:
                max_progress = 0.80

            if progress >= max_progress:
                return _block_signal(
                    signal,
                    reason=(
                        f"Stale signal: price already {progress:.0%} to TP "
                        f"(limit {max_progress:.0%})"
                    ),
                    decision="blocked_by_filter",
                    filter_name="stale_signal",
                    filters={
                        "progress_to_tp": progress,
                        "max_progress_to_tp": max_progress,
                        "trading_type": signal.get("trading_mode", ""),
                    },
                )

        except Exception as exc:
            logger.debug("Stale-entry guard skipped: %s", exc)

        return None

    def _revalidate_lot(self, signal: dict[str, Any], entry_raw: Any) -> float:
        lot = _safe_float(signal.get("lot_size"), 0.01)

        try:
            risk_manager = _current_risk_manager()

            if risk_manager is None:
                return lot

            account = self.client.get_account_info()
            if not account or not account.get("balance"):
                return lot

            current_balance = float(account["balance"])
            symbol_info = self.client.get_symbol_info(signal["symbol"])
            sl = _safe_float(signal.get("sl"), 0.0)
            entry = _safe_float(entry_raw, 0.0)

            if not sl or not entry or not symbol_info:
                return lot

            new_lot = risk_manager.calculate_lot_size(
                balance=current_balance,
                entry=entry,
                sl=sl,
                tick_value=symbol_info.get("pip_value", 1.0),
                tick_size=symbol_info.get("tick_size", 0.00001),
                min_lot=symbol_info.get("min_lot", 0.01),
                max_lot=symbol_info.get("max_lot", 100.0),
                lot_step=symbol_info.get("lot_step", 0.01),
            )

            try:
                from ai.rl_agent import rl_manager
                import math

                risk_factor = rl_manager.risk_factor(
                    signal.get("strategy", ""),
                    signal.get("trading_mode", "day_trading"),
                )

                if risk_factor != 1.0:
                    step = symbol_info.get("lot_step", 0.01)
                    min_lot = symbol_info.get("min_lot", 0.01)

                    adjusted_lot = round(math.floor(new_lot * risk_factor / step) * step, 8)

                    if adjusted_lot < min_lot:
                        signal["rejection_reason"] = (
                            f"RL risk factor {risk_factor:.2f} reduced revalidated lot "
                            f"below broker min_lot={min_lot}; rejected"
                        )
                        return 0.0

                    new_lot = adjusted_lot

            except Exception:
                pass

            if abs(new_lot - lot) / max(lot, 1e-8) > 0.10:
                logger.info(
                    "Lot revalidated for stale signal %s: %s → %s (balance=%.2f)",
                    signal.get("id", "?"),
                    lot,
                    new_lot,
                    current_balance,
                )
                lot = new_lot
                signal["lot_size"] = new_lot

        except Exception as exc:
            logger.debug("Lot revalidation skipped: %s", exc)

        return lot

    def _check_spread_guard(
        self,
        signal: dict[str, Any],
        trading_mode: str,
    ) -> ExecutionResult | None:
        try:
            spread_info = self.client.get_symbol_info(signal["symbol"])

            if not spread_info:
                return None

            live_spread = spread_info.get("spread_pips", 0.0)

            try:
                import json
                import os

                scanner_cfg_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config",
                    "market_scanner.json",
                )

                with open(scanner_cfg_path, encoding="utf-8") as file:
                    scanner_cfg = json.load(file)

                max_spread = (
                    scanner_cfg.get("trading_types", {})
                    .get(trading_mode, {})
                    .get("criteria", {})
                    .get("max_spread_pips", 999.0)
                )
            except Exception:
                max_spread = 999.0

            if live_spread > max_spread:
                reason = (
                    f"Live spread {live_spread:.2f} pips exceeds "
                    f"max {max_spread:.2f} pips for {trading_mode} "
                    f"— order blocked to protect funds"
                )

                notification_manager.add(
                    type="risk_alert",
                    title="High Spread — Trade Blocked",
                    message=(
                        f"{signal.get('symbol')} {str(signal.get('direction', '')).upper()}: "
                        f"spread {live_spread:.1f} pips (max {max_spread:.1f}) "
                        f"— signal rejected to protect funds"
                    ),
                    severity="warning",
                    metadata={
                        "symbol": signal.get("symbol"),
                        "trading_mode": trading_mode,
                        "live_spread_pips": live_spread,
                        "max_spread_pips": max_spread,
                        "signal_id": signal.get("id"),
                    },
                )

                return _block_signal(
                    signal,
                    reason=reason,
                    decision="blocked_by_filter",
                    filter_name="spread_guard",
                    filters={
                        "spread_pips": live_spread,
                        "max_spread_pips": max_spread,
                    },
                )

        except Exception as exc:
            logger.debug("Spread guard skipped: %s", exc)

        return None

    @staticmethod
    def _is_ea_enabled() -> bool:
        try:
            return bool(_get_bus_app_cfg().get("ea_enabled", False))
        except Exception:
            return False
