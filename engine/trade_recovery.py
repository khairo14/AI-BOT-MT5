from __future__ import annotations

"""
Trade recovery and MT5 reconciliation for EVOTRADE AI.

Responsibilities:
- recover journal-open trades that closed while API was offline
- re-launch lifecycle polling for still-open trades
- detect live MT5 positions missing journal open entries
- write recovery close/open journal records
- pass recovered close outcomes through centralized learning pipeline

This module is intended to replace recover_unclosed_trades() from api.signal_bus.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from ai.trade_memory import TradeOutcome, memory
from engine.trade_identity import current_trade_identity, enrich_with_trade_identity
from engine.trade_learning import apply_trade_learning
from engine.trade_lifecycle import already_closed, mark_trade_closed, mark_trade_open
from engine.trade_state import trade_state_store
from engine.utils.symbol_utils import normalize_symbol

logger = logging.getLogger(__name__)


def get_server_utc_offset_secs(symbol: str = "EURUSD") -> int:
    """
    Detect MT5 server-clock offset from UTC.

    Always probes liquid forex symbols rather than the trade symbol because
    commodities/indices can have stale weekend ticks.
    """
    import MetaTrader5 as mt5

    forex_probes = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF")

    tick = None
    for probe in forex_probes:
        try:
            tick = getattr(mt5, "symbol_info_tick")(probe)
        except Exception:
            tick = None

        if tick is not None:
            break

    if tick is None:
        return 0

    diff = int(getattr(tick, "time", 0) or 0) - int(time.time())

    if abs(diff) > 14 * 3600:
        return 0

    return round(diff / 3600) * 3600


def _direction_from_position(pos: dict[str, Any]) -> str:
    raw = pos.get("type", "")

    if isinstance(raw, str):
        upper = raw.upper()
        if upper in {"BUY", "SELL"}:
            return upper.lower()

    try:
        return "buy" if int(raw) == 0 else "sell"
    except Exception:
        return "buy"


def _trading_type_from_comment(comment: str) -> str:
    comment_lower = str(comment or "").lower()

    if comment_lower.startswith("scalp"):
        return "scalping"

    if comment_lower.startswith("swing"):
        return "swing"

    return "day_trading"


def _classify_outcome(
    *,
    direction: str,
    close_price: float,
    sl: float,
    tp: float,
    profit: float,
    pip_value: float,
) -> str:
    direction = direction.upper()
    tol = max(abs(close_price) * 0.0001, pip_value * 2)

    if direction == "BUY":
        if tp and close_price >= tp - tol:
            return "tp_hit"
        if sl and close_price <= sl + tol:
            return "sl_hit"
    else:
        if tp and close_price <= tp + tol:
            return "tp_hit"
        if sl and close_price >= sl - tol:
            return "sl_hit"

    if profit > 0:
        return "tp_hit"

    if profit < 0:
        return "sl_hit"

    return "manual_close"


def _safe_iso_from_timestamp(ts: int | float, *, offset_secs: int = 0) -> str:
    return datetime.fromtimestamp(float(ts) - offset_secs, tz=timezone.utc).isoformat()


async def recover_unclosed_trades(client, *, poll_callback=None) -> None:
    """
    Recover trades that were open in journal but closed while API was offline.

    poll_callback:
        async callable(ticket=int, signal=dict, client=client)
        Used to re-launch lifecycle polling for still-open trades.
    """
    import MetaTrader5 as mt5
    from engine.trade_journal import trade_journal
    from engine.account_store import current_mode, current_account_login

    unclosed = trade_journal.get_unclosed_tickets()

    if not unclosed:
        return

    logger.info(
        "Recovery: found %s unclosed journal ticket(s): %s",
        len(unclosed),
        [entry.get("ticket") for entry in unclosed],
    )

    live_raw = await asyncio.to_thread(client.get_open_positions)
    live_positions = live_raw or []
    live_tickets = {int(p["ticket"]) for p in live_positions if p.get("ticket") is not None}

    server_offset = await asyncio.to_thread(get_server_utc_offset_secs)

    logger.info("Recovery: detected server UTC offset = %+dh", server_offset // 3600)

    for entry in unclosed:
        ticket = int(entry["ticket"])
        identity = current_trade_identity(entry)

        if ticket in live_tickets:
            continue

        if already_closed(ticket, source=entry):
            logger.info(
                "Recovery: skip already closed state %s:%s:%s",
                identity.account_mode,
                identity.account_login,
                ticket,
            )
            continue

        deals = await asyncio.to_thread(client.get_deals_by_position, ticket)
        deals = deals or []

        symbol = entry.get("symbol", "")
        closed = [d for d in deals if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]

        if not closed:
            logger.warning(
                "Recovery: no close deal found for ticket #%s (%s) — skipping",
                ticket,
                symbol,
            )
            continue

        deal = closed[-1]
        close_time = _safe_iso_from_timestamp(getattr(deal, "time", 0), offset_secs=server_offset)

        profit = float(getattr(deal, "profit", 0.0) or 0.0)
        if len(closed) > 1:
            profit = float(sum(float(getattr(d, "profit", 0.0) or 0.0) for d in closed))

        close_price = float(getattr(deal, "price", 0.0) or 0.0)
        entry_price = float(entry.get("entry") or 0.0)
        direction = str(entry.get("direction", "buy")).upper()
        trading_type = entry.get("trading_mode") or entry.get("trading_type") or "day_trading"

        pip_value = 0.01 if "JPY" in str(symbol).upper() else 0.0001
        pips = ((close_price - entry_price) if direction == "BUY" else (entry_price - close_price)) / pip_value

        sl = float(entry.get("sl") or 0.0)
        tp = float(entry.get("tp") or 0.0)

        outcome_type = _classify_outcome(
            direction=direction,
            close_price=close_price,
            sl=sl,
            tp=tp,
            profit=profit,
            pip_value=pip_value,
        )

        open_time = entry.get("open_time", close_time)
        try:
            open_dt = datetime.fromisoformat(str(open_time).replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_time)
            duration_mins = (close_dt - open_dt).total_seconds() / 60
        except Exception:
            duration_mins = 0.0

        # Write close journal once; lifecycle guard is checked above.
        trade_journal.log(
            ticket=ticket,
            symbol=symbol,
            strategy=entry.get("strategy") or entry.get("comment", ""),
            account_type=identity.account_type,
            user_id=identity.user_id,
            direction=entry.get("direction", "buy"),
            volume=float(entry.get("volume") or 0.01),
            entry=entry_price,
            sl=sl,
            tp=tp if tp else None,
            profit=profit,
            trading_type=trading_type,
            account_mode=identity.account_mode,
            account_login=identity.account_login,
            comment=entry.get("comment", ""),
            event="close",
            close_time=close_time,
            swap=getattr(deal, "swap", None),
            commission=getattr(deal, "commission", None),
        )

        try:
            risk_balance = 0.0
            try:
                from api.runner_loop import _risk_manager

                risk_balance = float((_risk_manager._day_start_balance if _risk_manager else 0.0) or 0.0)
            except Exception:
                risk_balance = 0.0

            profit_pct = (profit / risk_balance * 100.0) if risk_balance > 0 else (profit / 10000.0 * 100.0)

            stats = memory.stats(
                trading_type=trading_type,
                live_only=True,
                mode=identity.account_mode,
                exclude_manual=True,
                account_login=identity.account_login,
            )

            rl_state = None
            try:
                from ai.rl_agent import rl_manager

                rl_state = rl_manager.get_state(
                    trading_type=trading_type,
                    win_rate=stats.get("win_rate", 0.5),
                    avg_conf=stats.get("avg_conf", 0.5),
                    drawdown_pct=0.0,
                    vol_pct=abs(entry_price - sl) / entry_price * 100.0 if entry_price > 0 else 0.5,
                    strategy_name=entry.get("strategy") or entry.get("comment") or None,
                )
            except Exception:
                rl_state = None

            outcome = TradeOutcome(
                ticket=ticket,
                symbol=symbol,
                symbol_raw=entry.get("symbol_raw") or symbol,
                symbol_normalized=entry.get("symbol_normalized") or normalize_symbol(symbol),
                account_type=identity.account_type,
                account_login=identity.account_login,
                execution_mode=identity.account_mode,
                source=identity.account_mode,
                user_id=identity.user_id,
                strategy=entry.get("strategy") or entry.get("comment", "unknown"),
                trading_type=trading_type,
                direction=direction,
                confidence=float(entry.get("confidence") or 0.5),
                entry_price=entry_price,
                close_price=close_price,
                sl_price=sl,
                tp_price=tp,
                volume=float(entry.get("volume") or 0.01),
                profit=profit,
                profit_pips=round(pips, 1),
                profit_pct=profit_pct,
                outcome=outcome_type,
                open_time=open_time,
                close_time=close_time,
                duration_mins=round(duration_mins, 1),
                mode=trading_type,
                lstm_predicted_direction=None,
                regime=entry.get("regime"),
                rl_state=rl_state,
                extra={"source": "recovery", "slippage_pips": 0.0},
            )

            newly_closed = mark_trade_closed(
                ticket,
                source=entry,
                close_time=close_time,
                outcome=outcome_type,
                profit=profit,
                payload={"last_event": "recovered_closed"},
            )

            if not newly_closed:
                logger.info(
                    "Recovery: duplicate lifecycle close ignored %s:%s:%s",
                    identity.account_mode,
                    identity.account_login,
                    ticket,
                )
                continue

            apply_trade_learning(
                outcome,
                win_rate=stats.get("win_rate", 0.5),
                avg_conf=stats.get("avg_conf", 0.5),
                drawdown_pct=0.0,
                vol_pct=abs(entry_price - sl) / max(abs(entry_price), 1e-8) * 100.0 if entry_price and sl else 0.0,
            )

        except Exception as exc:
            logger.exception("Recovery: trade learning failed for #%s: %s", ticket, exc)
            
        logger.info(
            "Recovery: backfilled close for #%s %s %s profit=%.2f",
            ticket,
            symbol,
            direction,
            profit,
        )

    # Re-launch poll for journal-open tickets that are still live.
    if poll_callback is not None:
        still_open = [entry for entry in unclosed if int(entry["ticket"]) in live_tickets]

        for entry in still_open:
            fake_signal = {
                "symbol": entry.get("symbol"),
                "direction": entry.get("direction", "buy"),
                "trading_mode": entry.get("trading_mode") or entry.get("trading_type") or "day_trading",
                "strategy": entry.get("strategy") or entry.get("comment", ""),
                "fill_price": entry.get("entry"),
                "entry_price": entry.get("entry"),
                "sl": entry.get("sl"),
                "tp": entry.get("tp"),
                "tp2": entry.get("tp2"),
                "tp3": entry.get("tp3"),
                "lot_size": entry.get("volume"),
                "confidence": float(entry.get("confidence") or 0.5),
                "account_mode": entry.get("account_mode") or current_mode(),
                "account_login": int(entry.get("account_login") or current_account_login() or 0),
                "account_type": entry.get("account_type") or entry.get("account_mode") or current_mode(),
                "user_id": entry.get("user_id", "default"),
            }

            await poll_callback(ticket=int(entry["ticket"]), signal=fake_signal, client=client)

            logger.info(
                "Recovery: re-launched poll for still-open #%s %s",
                entry["ticket"],
                entry.get("symbol"),
            )

    # Reverse reconciliation: MT5 live positions with no journal open entry.
    journal_open_tickets = {int(entry["ticket"]) for entry in unclosed}
    untracked = [
        pos for pos in live_positions
        if int(pos.get("ticket") or 0) not in journal_open_tickets
    ]

    for pos in untracked:
        ticket = int(pos["ticket"])
        symbol = pos.get("symbol", "")
        direction = _direction_from_position(pos)
        entry_price = float(pos.get("open_price") or pos.get("price_open") or 0.0)
        sl = float(pos.get("sl") or 0.0)
        tp = float(pos.get("tp") or 0.0)
        volume = float(pos.get("volume") or 0.01)
        comment = pos.get("comment", "")
        trading_type = _trading_type_from_comment(comment)

        source = enrich_with_trade_identity({
            "account_mode": current_mode(),
            "account_login": current_account_login(),
            "account_type": current_mode(),
            "user_id": "default",
        })

        try:
            trade_journal.log(
                ticket=ticket,
                symbol=symbol,
                direction=direction,
                volume=volume,
                entry=entry_price,
                sl=sl,
                tp=tp if tp else None,
                profit=None,
                trading_type=trading_type,
                account_mode=source["account_mode"],
                account_login=source["account_login"],
                account_type=source["account_type"],
                user_id=source["user_id"],
                comment=comment,
                event="open",
            )
        except Exception as exc:
            logger.exception("Recovery: journal open failed for untracked #%s: %s", ticket, exc)

        mark_trade_open(
            ticket,
            source=source,
            payload={
                "symbol_raw": symbol,
                "symbol_normalized": normalize_symbol(symbol),
                "mode": trading_type,
                "strategy": comment,
                "direction": direction.upper(),
                "entry": entry_price,
                "sl": sl,
                "tp1": tp if tp else None,
                "volume": volume,
                "last_event": "recovered_open",
            },
        )

        fake_signal = {
            "symbol": symbol,
            "direction": direction,
            "trading_mode": trading_type,
            "strategy": comment,
            "fill_price": entry_price,
            "entry_price": entry_price,
            "sl": sl,
            "tp": tp,
            "lot_size": volume,
            "confidence": 0.5,
            "account_mode": source["account_mode"],
            "account_login": source["account_login"],
            "account_type": source["account_type"],
            "user_id": source["user_id"],
        }

        if poll_callback is not None:
            await poll_callback(ticket=ticket, signal=fake_signal, client=client)

        logger.info(
            "Recovery: found untracked live position #%s %s %s — wrote journal open-event",
            ticket,
            symbol,
            direction,
        )
