from __future__ import annotations

"""
Trade outcome polling for EVOTRADE AI.

Responsibilities:
- monitor open MT5 positions after execution/recovery
- handle TP1 partial close
- handle BE movement
- handle ATR trailing
- detect final close
- write close journal
- update lifecycle state
- pass validated close outcome to centralized learning

This module replaces api.signal_bus._poll_outcome().
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ai.trade_memory import TradeOutcome, memory
from engine.notification_manager import notification_manager
from engine.trade_identity import current_trade_identity
from engine.trade_learning import apply_trade_learning
from engine.trade_lifecycle import (
    already_closed,
    get_trade_state,
    mark_break_even,
    mark_tp1_hit,
    mark_trade_closed,
    mark_trailing_active,
)
from engine.trade_recovery import get_server_utc_offset_secs
from engine.utils.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)

MAX_POLLS_BY_TYPE = {
    "scalping": 2 * 24 * 120,
    "day_trading": 14 * 24 * 120,
    "swing": 45 * 24 * 120,
}


def _lstm_dir_from_signal(signal: dict[str, Any]) -> Optional[str]:
    raw = signal.get("indicators", {}).get("lstm_raw_prob")
    if raw is not None:
        return "BUY" if float(raw) > 0.5 else "SELL"

    direction = str(signal.get("direction", "")).upper()
    return direction or None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


async def poll_trade_outcome(ticket: int, signal: dict[str, Any], client) -> None:
    """
    Poll MT5 every 30s until a position closes, then record outcome once.
    """
    import MetaTrader5 as mt5

    identity = current_trade_identity(signal)
    trading_mode = signal.get("trading_mode", signal.get("trading_type", "day_trading"))

    lifecycle = get_trade_state(ticket, source=signal).state

    tp1_triggered = bool(lifecycle.get("tp1_hit", False))
    day_be_triggered = bool(lifecycle.get("day_be_triggered", False))
    swing_pre_tp1_be_triggered = bool(lifecycle.get("swing_pre_tp1_be_triggered", False))
    swing_be_triggered = bool(lifecycle.get("swing_be_triggered", False))

    max_polls = MAX_POLLS_BY_TYPE.get(trading_mode, 14 * 24 * 120)
    open_time = signal.get("open_time") or datetime.now(timezone.utc).isoformat()

    server_offset = await asyncio.to_thread(
        get_server_utc_offset_secs,
        signal.get("symbol", "EURUSD"),
    )

    symbol_digits = 5
    try:
        symbol_info = await asyncio.to_thread(
            client.get_symbol_info,
            signal.get("symbol", "EURUSD"),
        )
        if symbol_info:
            symbol_digits = int(symbol_info.get("digits", 5))
    except Exception:
        pass

    atr_cache: dict[str, tuple[float, float]] = {}

    for _ in range(max_polls):
        await asyncio.sleep(30)

        try:
            pos = await asyncio.to_thread(client.get_position_by_ticket, ticket)

            if pos:
                await _handle_open_position_lifecycle(
                    ticket=ticket,
                    signal=signal,
                    client=client,
                    pos=pos,
                    symbol_digits=symbol_digits,
                    atr_cache=atr_cache,
                    tp1_triggered_ref=lambda: tp1_triggered,
                    set_tp1_triggered=lambda value: _set_nonlocal("tp1_triggered", value),
                )

                # Refresh persisted flags after lifecycle handling.
                lifecycle = get_trade_state(ticket, source=signal).state
                tp1_triggered = bool(lifecycle.get("tp1_hit", tp1_triggered))
                day_be_triggered = bool(lifecycle.get("day_be_triggered", day_be_triggered))
                swing_pre_tp1_be_triggered = bool(
                    lifecycle.get("swing_pre_tp1_be_triggered", swing_pre_tp1_be_triggered)
                )
                swing_be_triggered = bool(lifecycle.get("swing_be_triggered", swing_be_triggered))

                continue

            # Closed: load MT5 deal history by position ID.
            deals = await asyncio.to_thread(client.get_deals_by_position, ticket)
            deals = deals or []

            closed = [
                deal for deal in deals
                if getattr(deal, "entry", None) == mt5.DEAL_ENTRY_OUT
            ]

            if not closed:
                continue

            if already_closed(ticket, source=signal):
                logger.warning(
                    "Duplicate close ignored in outcome poller: %s:%s:%s",
                    identity.account_mode,
                    identity.account_login,
                    ticket,
                )
                return

            await _process_closed_position(
                ticket=ticket,
                signal=signal,
                client=client,
                closed_deals=closed,
                server_offset=server_offset,
                symbol_digits=symbol_digits,
                open_time=open_time,
                tp1_triggered=tp1_triggered,
            )

            return

        except Exception as exc:
            logger.warning("_poll_outcome error for #%s: %s", ticket, exc)
            await asyncio.sleep(60)


def _set_nonlocal(_name: str, _value: bool) -> None:
    """
    Placeholder hook retained for readability.

    The poll loop refreshes flags from TradeStateStore after each lifecycle pass,
    so explicit nonlocal mutation is not required.
    """
    return None


async def _handle_open_position_lifecycle(
    *,
    ticket: int,
    signal: dict[str, Any],
    client,
    pos,
    symbol_digits: int,
    atr_cache: dict[str, tuple[float, float]],
    tp1_triggered_ref,
    set_tp1_triggered,
) -> None:
    """
    Handle TP1, BE, and trailing while position remains open.
    """
    direction = str(signal.get("direction", "")).upper()
    entry_px = _safe_float(signal.get("fill_price") or signal.get("entry_price"), 0.0)
    orig_sl = _safe_float(signal.get("sl"), 0.0)
    tp2 = _safe_float(signal.get("tp2"), 0.0)
    tp1 = _safe_float(signal.get("tp"), 0.0)
    trading_mode = signal.get("trading_mode", signal.get("trading_type", ""))

    lifecycle = get_trade_state(ticket, source=signal).state

    tp1_triggered = bool(lifecycle.get("tp1_hit", False))
    day_be_triggered = bool(lifecycle.get("day_be_triggered", False))
    swing_pre_tp1_be_triggered = bool(lifecycle.get("swing_pre_tp1_be_triggered", False))
    swing_be_triggered = bool(lifecycle.get("swing_be_triggered", False))

    def _get_om():
        try:
            from api.main import get_mt5_client
            from engine.order_manager import OrderManager

            current_client = get_mt5_client()
            return OrderManager(current_client) if current_client else None
        except Exception:
            return None

    async def _try_trail(new_sl: float) -> None:
        om = _get_om()

        if not om:
            return

        rounded = round(new_sl, symbol_digits)

        if direction == "BUY" and rounded > pos.sl + 1e-9:
            ok = await asyncio.to_thread(om.modify_position, ticket, rounded)
            if ok:
                mark_trailing_active(ticket, source=signal)
                logger.info("Trail SL → %s | #%s %s", rounded, ticket, signal.get("symbol"))

        elif direction == "SELL" and (pos.sl == 0 or rounded < pos.sl - 1e-9):
            ok = await asyncio.to_thread(om.modify_position, ticket, rounded)
            if ok:
                mark_trailing_active(ticket, source=signal)
                logger.info("Trail SL → %s | #%s %s", rounded, ticket, signal.get("symbol"))

    async def _get_atr(timeframe: str, period: int = 14) -> float:
        now_mono = time.monotonic()
        cached = atr_cache.get(timeframe)

        if cached and now_mono - cached[1] < 300:
            return cached[0]

        try:
            import pandas as pd

            df = await asyncio.to_thread(
                client.get_ohlcv,
                signal.get("symbol", ""),
                timeframe,
                period + 6,
            )

            if df is None or len(df) < period + 1:
                return 0.0

            high = df["high"]
            low = df["low"]
            prev = df["close"].shift(1)

            tr = pd.concat(
                [
                    high - low,
                    (high - prev).abs(),
                    (low - prev).abs(),
                ],
                axis=1,
            ).max(axis=1)

            val = float(tr.rolling(period).mean().iloc[-1])
            val = val if val > 0 else 0.0
            atr_cache[timeframe] = (val, now_mono)

            return val

        except Exception:
            return 0.0

    # TP1 partial close for day/swing with tp2.
    if tp2 and tp1 and not tp1_triggered and trading_mode != "scalping":
        hit_tp1 = (
            (direction == "BUY" and pos.price_current >= tp1)
            or (direction == "SELL" and pos.price_current <= tp1)
        )

        if hit_tp1:
            try:
                om = _get_om()

                if om and entry_px:
                    partial_pct = 0.4 if trading_mode == "swing" else 0.5
                    partial_ok = await asyncio.to_thread(om.partial_close, ticket, partial_pct)

                    if partial_ok:
                        mark_tp1_hit(
                            ticket,
                            source=signal,
                            extra={
                                "day_be_triggered": True,
                                "break_even_moved": True,
                                "last_event": "tp1_partial_close",
                            },
                        )

                        try:
                            from engine.trade_state import trade_state_store

                            identity = current_trade_identity(signal)
                            trade_state_store.mark_partial_close(
                                identity.account_mode,
                                identity.account_login,
                                ticket,
                                pct=partial_pct,
                                reason="tp1",
                            )
                        except Exception:
                            pass

                        await asyncio.to_thread(om.modify_position, ticket, entry_px, tp2)

                    logger.info(
                        "TP1 partial-close fired: #%s %s %s%% closed, BE=%s → TP2=%s",
                        ticket,
                        signal.get("symbol"),
                        int(partial_pct * 100),
                        entry_px,
                        tp2,
                    )

                    _notify_partial_tp1(ticket, signal, tp1, tp2, partial_pct, entry_px)

            except Exception as exc:
                logger.warning("TP1 partial-close failed #%s: %s", ticket, exc)

    # Day-trading halfway BE before TP1.
    if (
        trading_mode not in ("scalping", "swing")
        and tp1
        and entry_px
        and orig_sl
        and not tp1_triggered
        and not day_be_triggered
    ):
        halfway = (
            entry_px + (tp1 - entry_px) * 0.5
            if direction == "BUY"
            else entry_px - (entry_px - tp1) * 0.5
        )

        hit_halfway = (
            (direction == "BUY" and pos.price_current >= halfway)
            or (direction == "SELL" and pos.price_current <= halfway)
        )

        if hit_halfway:
            await _move_to_be_if_improves(ticket, signal, pos, entry_px, direction, event_key="day_be_triggered")

    # Swing pre-TP1 BE.
    if (
        trading_mode == "swing"
        and tp1
        and tp2
        and entry_px
        and orig_sl
        and not tp1_triggered
        and not swing_pre_tp1_be_triggered
    ):
        halfway = (
            entry_px + (tp1 - entry_px) * 0.5
            if direction == "BUY"
            else entry_px - (entry_px - tp1) * 0.5
        )

        hit_halfway = (
            (direction == "BUY" and pos.price_current >= halfway)
            or (direction == "SELL" and pos.price_current <= halfway)
        )

        if hit_halfway:
            await _move_to_be_if_improves(
                ticket,
                signal,
                pos,
                entry_px,
                direction,
                event_key="swing_pre_tp1_be_triggered",
            )

    # ATR trail after TP1/day-BE/swing-pre-TP1-BE.
    lifecycle = get_trade_state(ticket, source=signal).state
    tp1_triggered = bool(lifecycle.get("tp1_hit", tp1_triggered))
    day_be_triggered = bool(lifecycle.get("day_be_triggered", day_be_triggered))
    swing_pre_tp1_be_triggered = bool(
        lifecycle.get("swing_pre_tp1_be_triggered", swing_pre_tp1_be_triggered)
    )

    if (
        (tp1_triggered or day_be_triggered or swing_pre_tp1_be_triggered)
        and entry_px
        and orig_sl
        and trading_mode != "scalping"
    ):
        try:
            if trading_mode == "swing":
                atr = await _get_atr("H4", 14)
                trail_dist = atr * 2.0 if atr > 0 else abs(entry_px - orig_sl) * 0.5
            else:
                atr = await _get_atr("H1", 14)
                trail_dist = atr * 1.5 if atr > 0 else abs(entry_px - orig_sl) * 0.5

            new_sl = (
                max(pos.price_current - trail_dist, entry_px)
                if direction == "BUY"
                else min(pos.price_current + trail_dist, entry_px)
            )

            await _try_trail(new_sl)

        except Exception as exc:
            logger.debug("%s trail failed #%s: %s", "Swing" if trading_mode == "swing" else "Day", ticket, exc)

    # Single-target swing: halfway BE then ATR trail.
    elif not tp2 and tp1 and entry_px and orig_sl:
        halfway = (
            entry_px + (tp1 - entry_px) * 0.5
            if direction == "BUY"
            else entry_px - (entry_px - tp1) * 0.5
        )

        if not swing_be_triggered:
            hit_halfway = (
                (direction == "BUY" and pos.price_current >= halfway)
                or (direction == "SELL" and pos.price_current <= halfway)
            )

            if hit_halfway:
                await _move_to_be_if_improves(
                    ticket,
                    signal,
                    pos,
                    entry_px,
                    direction,
                    event_key="swing_be_triggered",
                )

        lifecycle = get_trade_state(ticket, source=signal).state
        swing_be_triggered = bool(lifecycle.get("swing_be_triggered", swing_be_triggered))

        if swing_be_triggered:
            try:
                atr = await _get_atr("H4", 14)
                trail_dist = atr * 2.0 if atr > 0 else abs(entry_px - orig_sl) * 0.5
                new_sl = (
                    max(pos.price_current - trail_dist, entry_px)
                    if direction == "BUY"
                    else min(pos.price_current + trail_dist, entry_px)
                )

                await _try_trail(new_sl)

            except Exception as exc:
                logger.debug("Swing trail failed #%s: %s", ticket, exc)


async def _move_to_be_if_improves(
    ticket: int,
    signal: dict[str, Any],
    pos,
    entry_px: float,
    direction: str,
    *,
    event_key: str,
) -> None:
    try:
        from api.main import get_mt5_client
        from engine.order_manager import OrderManager

        current_client = get_mt5_client()
        om = OrderManager(current_client) if current_client else None

        if not om:
            return

        be_improves = (
            (direction == "BUY" and entry_px > pos.sl + 1e-9)
            or (direction == "SELL" and (pos.sl == 0 or entry_px < pos.sl - 1e-9))
        )

        if not be_improves:
            return

        be_ok = await asyncio.to_thread(om.modify_position, ticket, entry_px)

        if be_ok:
            mark_break_even(
                ticket,
                source=signal,
                extra={
                    event_key: True,
                    "last_event": event_key.replace("_triggered", ""),
                },
            )

            logger.info(
                "%s fired: #%s %s SL → entry %s",
                event_key,
                ticket,
                signal.get("symbol"),
                entry_px,
            )

    except Exception as exc:
        logger.warning("%s failed #%s: %s", event_key, ticket, exc)


def _notify_partial_tp1(
    ticket: int,
    signal: dict[str, Any],
    tp1: float,
    tp2: float,
    partial_pct: float,
    entry_px: float,
) -> None:
    try:
        pct_str = f"{int(partial_pct * 100)}%"

        notification_manager.add(
            type="position_closed",
            title=f"Partial TP1 — {signal.get('symbol')}",
            message=(
                f"{str(signal.get('direction', '')).upper()} {signal.get('symbol')} "
                f"#{ticket}: {pct_str} closed at TP1={tp1:.5g}, "
                f"SL → BE={entry_px:.5g}, trailing to TP2={tp2:.5g}"
            ),
            severity="success",
            metadata={
                "ticket": ticket,
                "symbol": signal.get("symbol"),
                "direction": signal.get("direction"),
                "tp1": tp1,
                "tp2": tp2,
                "partial_pct": partial_pct,
                "trading_mode": signal.get("trading_mode"),
            },
        )

        from api.websocket.feed import manager as ws_manager

        asyncio.create_task(
            ws_manager.broadcast_alert({
                "type": "position_closed",
                "symbol": signal.get("symbol"),
                "profit": 0.0,
                "pips": 0.0,
                "outcome": "tp_hit",
                "strategy": signal.get("strategy", ""),
                "partial": True,
                "partial_pct": partial_pct,
            })
        )

    except Exception:
        pass


async def _process_closed_position(
    *,
    ticket: int,
    signal: dict[str, Any],
    client,
    closed_deals: list[Any],
    server_offset: int,
    symbol_digits: int,
    open_time: str,
    tp1_triggered: bool,
) -> None:
    deal = closed_deals[-1]

    profit = float(getattr(deal, "profit", 0.0) or 0.0)
    if len(closed_deals) > 1:
        profit = float(sum(float(getattr(d, "profit", 0.0) or 0.0) for d in closed_deals))

    close_px = float(getattr(deal, "price", 0.0) or 0.0)
    entry_px = _safe_float(signal.get("fill_price") or signal.get("entry_price"), 0.0)
    symbol = signal["symbol"]
    direction = str(signal["direction"]).upper()
    pip_value = 0.0001 if "JPY" not in str(symbol).upper() else 0.01

    pips = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_value

    close_time = datetime.fromtimestamp(
        float(getattr(deal, "time", 0.0) or 0.0) - server_offset,
        tz=timezone.utc,
    ).isoformat()

    sl = _safe_float(signal.get("sl"), 0.0)
    tp = _safe_float(signal.get("tp"), 0.0)
    tp2 = _safe_float(signal.get("tp2"), 0.0)
    check_tp = tp2 if tp1_triggered and tp2 else tp

    outcome_type = _classify_outcome(
        direction=direction,
        close_px=close_px,
        sl=sl,
        tp=check_tp,
        profit=profit,
        pip_value=pip_value,
    )

    try:
        open_dt = datetime.fromisoformat(str(open_time).replace("Z", "+00:00"))
        close_dt = datetime.fromisoformat(close_time)
        duration_mins = (close_dt - open_dt).total_seconds() / 60
    except Exception:
        duration_mins = 0.0

    pre_balance = None
    try:
        from api.runner_loop import _risk_manager

        pre_balance = _risk_manager._day_start_balance if _risk_manager else None
    except Exception:
        pre_balance = None

    mem_profit_pct = (
        profit / pre_balance * 100.0
        if pre_balance and pre_balance > 0
        else profit / 10_000.0 * 100.0
    )

    slippage_pips = _compute_slippage_pips(signal, symbol, symbol_digits, pip_value)

    identity = current_trade_identity(signal)

    stats_mode = identity.account_mode
    trading_type = signal.get("trading_mode", "day_trading")

    stats = memory.stats(
        trading_type=trading_type,
        live_only=True,
        mode=stats_mode,
        exclude_manual=True,
        account_login=identity.account_login,
    )

    drawdown_pct, profit_pct = await _update_risk_manager_and_profit_pct(
        signal=signal,
        client=client,
        profit=profit,
        trading_type=trading_type,
        identity_mode=identity.account_mode,
    )

    rl_state = None
    try:
        from ai.rl_agent import rl_manager

        vol_pct = abs(entry_px - sl) / max(abs(entry_px), 1e-8) * 100 if entry_px and sl else 0.0

        rl_state = rl_manager.get_state(
            trading_type=trading_type,
            win_rate=stats.get("win_rate", 0.5),
            avg_conf=stats.get("avg_conf", 0.5),
            drawdown_pct=drawdown_pct,
            vol_pct=vol_pct,
            strategy_name=signal.get("strategy") or None,
        )
    except Exception as exc:
        logger.debug("RL state capture failed: %s", exc)

    outcome = TradeOutcome(
        ticket=ticket,
        symbol=signal.get("symbol", ""),
        symbol_raw=signal.get("symbol_raw") or signal.get("symbol", ""),
        symbol_normalized=signal.get("symbol_normalized") or normalize_symbol(signal.get("symbol", "")),
        account_type=identity.account_type,
        account_login=identity.account_login,
        execution_mode=identity.account_mode,
        source=identity.account_mode,
        user_id=identity.user_id,
        strategy=signal.get("strategy") or "unknown",
        trading_type=trading_type,
        direction=direction,
        confidence=float(signal.get("confidence") or 0.5),
        entry_price=entry_px,
        close_price=close_px,
        sl_price=sl,
        tp_price=check_tp,
        volume=float(signal.get("lot_size", 0.01)),
        profit=profit,
        profit_pips=round(pips, 1),
        profit_pct=mem_profit_pct,
        outcome=outcome_type,
        open_time=open_time,
        close_time=close_time,
        duration_mins=round(duration_mins, 1),
        mode=trading_type,
        lstm_predicted_direction=_lstm_dir_from_signal(signal),
        regime=signal.get("regime"),
        rl_state=rl_state,
        extra={
            "source": identity.account_mode,
            "slippage_pips": slippage_pips,
            "lstm_raw_prob": signal.get("indicators", {}).get("lstm_raw_prob"),
        },
    )

    # Mark lifecycle closed before side-effects so duplicate poll/recovery loops stop.
    newly_closed = mark_trade_closed(
        ticket,
        source=signal,
        close_time=close_time,
        outcome=outcome_type,
        profit=profit,
    )

    if not newly_closed:
        return

    _write_close_journal(
        ticket=ticket,
        signal=signal,
        entry_px=entry_px,
        sl=sl,
        tp=tp,
        profit=profit,
        close_time=close_time,
        deal=deal,
    )

    apply_trade_learning(
        outcome,
        win_rate=stats.get("win_rate", 0.5),
        avg_conf=stats.get("avg_conf", 0.5),
        drawdown_pct=drawdown_pct,
        vol_pct=abs(entry_px - sl) / max(abs(entry_px), 1e-8) * 100 if entry_px and sl else 0.0,
    )

    _notify_final_close(
        ticket=ticket,
        signal=signal,
        direction=direction,
        profit=profit,
        pips=pips,
        outcome_type=outcome_type,
        close_px=close_px,
        trading_type=trading_type,
    )

    await _maybe_trigger_accuracy_retrain_and_optimizer(
        signal=signal,
        client=client,
        stats=stats,
        trading_type=trading_type,
        stats_mode=stats_mode,
    )


def _classify_outcome(
    *,
    direction: str,
    close_px: float,
    sl: float,
    tp: float,
    profit: float,
    pip_value: float,
) -> str:
    tol = max(abs(close_px) * 0.0001, pip_value * 2)

    if direction == "BUY":
        if tp and close_px >= tp - tol:
            return "tp_hit"
        if sl and close_px <= sl + tol:
            return "sl_hit"
    else:
        if tp and close_px <= tp + tol:
            return "tp_hit"
        if sl and close_px >= sl - tol:
            return "sl_hit"

    if profit > 0:
        return "tp_hit"

    if profit < 0:
        return "sl_hit"

    return "manual_close"


def _compute_slippage_pips(
    signal: dict[str, Any],
    symbol: str,
    symbol_digits: int,
    pip_value: float,
) -> float:
    signal_entry = _safe_float(signal.get("entry_price") or signal.get("entry"), 0.0)
    fill = _safe_float(signal.get("fill_price"), 0.0)

    if signal_entry <= 0 or fill <= 0:
        return 0.0

    symbol_upper = symbol.upper()

    if any(x in symbol_upper for x in ("BTC", "ETH", "SOL", "XRP", "XLM", "BNB", "LTC", "ADA")):
        slip_pip_value = 1.0
    elif any(x in symbol_upper for x in ("US30", "US100", "US500", "GER40", "UK100")):
        slip_pip_value = 1.0
    elif symbol_digits <= 2:
        slip_pip_value = 1.0
    elif symbol_digits in (5, 3):
        slip_pip_value = 0.01 if "JPY" in symbol_upper else 0.0001
    else:
        slip_pip_value = pip_value

    return round(abs(fill - signal_entry) / slip_pip_value, 2)


async def _update_risk_manager_and_profit_pct(
    *,
    signal: dict[str, Any],
    client,
    profit: float,
    trading_type: str,
    identity_mode: str,
) -> tuple[float, float]:
    drawdown_pct = 0.0
    profit_pct = profit

    try:
        from api.runner_loop import _risk_manager
        from engine.account_store import current_mode

        current_mode_value = current_mode()

        if _risk_manager is not None and current_mode_value == identity_mode:
            strategy_name = signal.get("strategy") or None

            if profit > 0:
                _risk_manager.record_win(trading_type, strategy_name=strategy_name)
            else:
                _risk_manager.record_loss(trading_type, strategy_name=strategy_name)

            if client and client.is_connected():
                account = await asyncio.to_thread(client.get_account_info)

                if account and account.get("balance"):
                    balance = float(account["balance"])
                    _risk_manager.update_balance(balance)

                    if _risk_manager._day_start_balance and _risk_manager._day_start_balance > 0:
                        drawdown_pct = max(
                            0.0,
                            (_risk_manager._day_start_balance - balance)
                            / _risk_manager._day_start_balance
                            * 100.0,
                        )

                    if balance > 0:
                        profit_pct = profit / balance * 100.0

    except Exception:
        pass

    return drawdown_pct, profit_pct


def _write_close_journal(
    *,
    ticket: int,
    signal: dict[str, Any],
    entry_px: float,
    sl: float,
    tp: float,
    profit: float,
    close_time: str,
    deal,
) -> None:
    try:
        from engine.trade_journal import trade_journal

        identity = current_trade_identity(signal)

        trade_journal.log(
            ticket=ticket,
            symbol=signal.get("symbol", ""),
            direction=signal.get("direction", ""),
            volume=float(signal.get("lot_size", 0.01)),
            entry=entry_px,
            sl=sl,
            tp=tp if tp else None,
            profit=profit,
            trading_type=signal.get("trading_mode", "day_trading"),
            account_mode=identity.account_mode,
            account_login=identity.account_login,
            account_type=identity.account_type,
            user_id=identity.user_id,
            comment=signal.get("strategy", ""),
            event="close",
            close_time=close_time,
            swap=getattr(deal, "swap", None),
            commission=getattr(deal, "commission", None),
            strategy=signal.get("strategy") or "",
        )

    except Exception as exc:
        logger.warning("Journal write failed for #%s: %s", ticket, exc)


def _notify_final_close(
    *,
    ticket: int,
    signal: dict[str, Any],
    direction: str,
    profit: float,
    pips: float,
    outcome_type: str,
    close_px: float,
    trading_type: str,
) -> None:
    try:
        labels = {
            "tp_hit": "✅ TP Hit",
            "sl_hit": "❌ SL Hit",
            "manual_close": "🔒 Closed",
        }
        outcome_label = labels.get(outcome_type, outcome_type)
        severity = "success" if profit > 0 else ("error" if profit < 0 else "info")

        notification_manager.add(
            type="position_closed",
            title=f"{outcome_label} — {signal['symbol']}",
            message=(
                f"{direction} {signal['symbol']} #{ticket} closed | "
                f"P&L: {profit:+.2f} | Pips: {pips:+.1f} | "
                f"{signal.get('trading_mode', trading_type)} via {signal.get('strategy', 'unknown')}"
            ),
            severity=severity,
            metadata={
                "ticket": ticket,
                "symbol": signal["symbol"],
                "direction": direction,
                "profit": profit,
                "pips": round(pips, 1),
                "outcome": outcome_type,
                "trading_mode": signal.get("trading_mode", trading_type),
                "close_price": close_px,
            },
        )
    except Exception:
        pass

    try:
        from api.websocket.feed import manager as ws_manager

        asyncio.create_task(
            ws_manager.broadcast_alert({
                "type": "position_closed",
                "symbol": signal["symbol"],
                "profit": round(profit, 2),
                "pips": round(pips, 1),
                "outcome": outcome_type,
                "strategy": signal.get("strategy", ""),
            })
        )
    except Exception:
        pass


async def _maybe_trigger_accuracy_retrain_and_optimizer(
    *,
    signal: dict[str, Any],
    client,
    stats: dict[str, Any],
    trading_type: str,
    stats_mode: str,
) -> None:
    """
    Preserve old signal_bus side effects:
    - LSTM accuracy snapshot
    - Auto LSTM retrain
    - Auto parameter optimizer
    """
    try:
        total = int(stats.get("total", 0) or 0)
        if total > 0 and total % 10 == 0:
            memory.snapshot_accuracy(
                trading_type=trading_type,
                min_samples=10,
                live_only=True,
                mode=stats_mode,
            )
    except Exception as exc:
        logger.warning("Accuracy snapshot failed: %s", exc)

    try:
        from ai.predictor import TRADING_TYPE_TF, predictor
        from ai.trade_memory import memory as trade_memory

        symbol = str(signal["symbol"]).rstrip("#+*!")
        key = f"{symbol}_{trading_type}"
        total = len([
            outcome for outcome in trade_memory.recent(n=500, live_only=True, learning_only=True)
            if str(outcome.get("symbol") or "").rstrip("#+*!") == symbol
            and outcome.get("trading_type") == trading_type
        ])

        retrain = False
        retrain_reason = ""

        from pathlib import Path

        models_dir = Path(__file__).parent.parent / "ai" / "models"
        model_file = models_dir / f"{key}_lstm_0.pt"
        legacy_model_file = models_dir / f"{key}_lstm.pt"

        no_model = (
            not model_file.exists()
            and not legacy_model_file.exists()
            and not predictor.is_training(symbol, trading_type)
        )

        if no_model:
            retrain = True
            retrain_reason = "no model — bootstrap"
        else:
            previous_total = total - 1
            retrain = (
                not predictor.is_training(symbol, trading_type)
                and total > 0
                and total % 20 == 0
                and previous_total % 20 != 0
            )
            retrain_reason = "20-trade window" if retrain else ""

        if not retrain and total >= 10 and not predictor.is_training(symbol, trading_type):
            metadata = predictor._metadata.get(key, {})
            trained_at = metadata.get("trained_at")
            if trained_at:
                try:
                    age_hours = (
                        datetime.now(timezone.utc)
                        - datetime.fromisoformat(trained_at)
                    ).total_seconds() / 3600

                    if age_hours > 7 * 24:
                        retrain = True
                        retrain_reason = f"stale model ({age_hours:.0f}h old)"
                except Exception:
                    pass

        if not retrain and not predictor.is_training(symbol, trading_type):
            recent_trades = [
                outcome for outcome in trade_memory.recent(n=50, live_only=True, learning_only=True)
                if str(outcome.get("symbol") or "").rstrip("#+*!") == symbol
                and outcome.get("trading_type") == trading_type
            ][-8:]

            if len(recent_trades) == 8 and all(outcome.get("profit", 0) < 0 for outcome in recent_trades):
                retrain = True
                retrain_reason = "8 consecutive losses"

        if retrain and client and client.is_connected():
            tf = TRADING_TYPE_TF.get(trading_type, "H1")

            if trading_type == "scalping":
                from datetime import timedelta

                date_to = datetime.now(timezone.utc)
                date_from = date_to - timedelta(days=868)
                df = await asyncio.to_thread(client.get_ohlcv_range, symbol, tf, date_from, date_to)
            else:
                bars = {
                    "scalping": 250_000,
                    "day_trading": 50_000,
                    "swing": 30_000,
                }.get(trading_type, 20_000)
                df = await asyncio.to_thread(client.get_ohlcv, symbol, tf, bars)

            if df is not None and not df.empty:
                predictor.train_async(symbol, df, trading_type)
                logger.info(
                    "Auto LSTM retrain triggered [%s]: %s (%s trades, %s bars)",
                    retrain_reason,
                    key,
                    total,
                    len(df),
                )

    except Exception as exc:
        logger.debug("Auto LSTM retrain skipped: %s", exc)

    try:
        from ai.param_optimizer import optimizer
        from ai.predictor import TRADING_TYPE_TF

        strategy = signal.get("strategy", "")
        symbol = signal["symbol"]

        if strategy and optimizer.should_reoptimize(strategy, symbol):
            tf = TRADING_TYPE_TF.get(trading_type, "H1")

            if client and client.is_connected():
                if trading_type == "scalping":
                    from datetime import timedelta

                    date_to = datetime.now(timezone.utc)
                    date_from = date_to - timedelta(days=868)
                    df = await asyncio.to_thread(client.get_ohlcv_range, symbol, tf, date_from, date_to)
                else:
                    bars = {
                        "scalping": 250_000,
                        "day_trading": 50_000,
                        "swing": 30_000,
                    }.get(trading_type, 20_000)
                    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf, bars)

                if df is not None and not df.empty:
                    optimizer.optimize_async(strategy, symbol, df, trading_type)
                    logger.info(
                        "Auto param optimizer triggered (%s bars): %s/%s",
                        len(df),
                        strategy,
                        symbol,
                    )

    except Exception as exc:
        logger.debug("Auto param optimizer skipped: %s", exc)
