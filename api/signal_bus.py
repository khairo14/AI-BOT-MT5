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
        self._client = None
        self._order_manager = None

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
                        # Broadcast expiry to dashboard
                        try:
                            if _event_loop and not _event_loop.is_closed():
                                asyncio.run_coroutine_threadsafe(
                                    self._broadcast_expired(dict(sig)), _event_loop
                                )
                        except Exception:
                            pass
                except Exception:
                    pass
        for sid in to_delete:
            self.queue.pop(sid, None)
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
                        return signal
        except Exception:
            pass

        self.queue[signal["id"]] = signal
        # Lazily purge stale + expired signals to keep queue bounded
        self.purge_stale()

        exec_mode = self._get_exec_mode(mode)
        if exec_mode == "auto" and self._order_manager is not None:
            signal["status"] = "executing"
            # Broadcast immediately so the dashboard card appears before execution
            asyncio.create_task(broadcast_signal(dict(signal)))
            asyncio.create_task(self._execute_async(signal))
        else:
            # Compute expiry for manual pending signals — keyed by timeframe, not mode
            try:
                exp_cfg = json.loads((CONFIG_DIR / "app.json").read_text()).get(
                    "signal_expiry_seconds", {}
                )
            except Exception:
                exp_cfg = {}
            tf = signal.get("timeframe", "")
            exp_secs = exp_cfg.get(tf) or _DEFAULT_EXPIRY.get(tf, 1800)
            from datetime import timedelta
            signal["expires_at"] = (
                datetime.now(tz=timezone.utc) + timedelta(seconds=exp_secs)
            ).isoformat()
            signal["status"] = "pending"
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
        # Status may already be "executing" if set by approve_signal under lock —
        # only update if it is still "pending" (direct call path).
        if signal.get("status") != "executing":
            signal["status"] = "executing"
        asyncio.create_task(self._execute_async(signal))
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

        # Notify dashboard of the outcome
        await broadcast_signal({**signal, "type": "signal_update"})

    def _do_execute_sync(self, signal: dict) -> bool:
        """Synchronous order placement — runs in a thread executor."""
        if self._order_manager is None or self._client is None:
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
            # Falls back to Python (place_market_order) automatically if the EA
            # is not active, times out, or returns an error.
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

            req = OrderRequest(
                symbol=signal["symbol"],
                direction=signal["direction"].upper(),
                volume=float(signal.get("lot_size") or 0.01),
                sl=float(signal["sl"]),
                tp=float(signal["tp"]) if signal.get("tp") else None,
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
            logger.exception(f"_do_execute_sync error: {exc}")
            return False

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _get_exec_mode(trading_type: str) -> str:
        try:
            cfg = json.loads((CONFIG_DIR / "app.json").read_text())
            return cfg.get("execution_mode", {}).get(trading_type, "manual")
        except Exception:
            return "manual"

    @staticmethod
    def _is_ea_enabled() -> bool:
        """Return True if ea_enabled=true in app.json."""
        try:
            cfg = json.loads((CONFIG_DIR / "app.json").read_text())
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

    Returns 0 if a tick cannot be obtained (safe default).
    """
    import MetaTrader5 as mt5
    import time as _time

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        # Try a fallback symbol
        for sym in ("GBPUSD", "USDJPY", "BTCUSD"):
            tick = mt5.symbol_info_tick(sym)
            if tick is not None:
                break
    if tick is None:
        return 0
    diff = tick.time - int(_time.time())
    # Round to nearest hour — offsets are always whole hours
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

        closed = [d for d in deals if d.entry == mt5.DEAL_ENTRY_OUT]
        if not closed:
            logger.debug(f"Recovery: no close deal found for ticket #{ticket} — skipping")
            continue

        deal      = closed[-1]
        # Correct deal.time for broker server-clock offset so we store true UTC
        close_time = datetime.fromtimestamp(deal.time - server_offset, tz=timezone.utc).isoformat()
        profit     = deal.profit
        close_px   = deal.price
        entry_px   = float(entry.get("entry") or 0)
        symbol     = entry.get("symbol", "")
        direction  = entry.get("direction", "buy").upper()
        trading_type = entry.get("trading_mode") or entry.get("trading_type") or "day_trading"
        pip_val    = 0.01 if "JPY" in symbol else 0.0001
        pips       = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_val

        sl = float(entry.get("sl") or 0)
        tp = float(entry.get("tp") or 0)
        tol = pip_val * 3
        if tp and abs(close_px - tp) <= tol:
            outcome_type = "tp_hit"
        elif sl and abs(close_px - sl) <= tol:
            outcome_type = "sl_hit"
        else:
            outcome_type = "manual_close"

        open_dt2  = datetime.fromisoformat(entry.get("open_time", close_time).replace("Z", "+00:00"))
        close_dt2 = datetime.fromtimestamp(deal.time, tz=timezone.utc)
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
            outcome = TradeOutcome(
                ticket=ticket,
                symbol=symbol,
                strategy=entry.get("comment", "unknown"),
                trading_type=trading_type,
                direction=direction,
                confidence=0.5,
                entry_price=entry_px,
                close_price=close_px,
                sl_price=sl,
                tp_price=tp,
                volume=float(entry.get("volume") or 0.01),
                profit=profit,
                profit_pips=round(pips, 1),
                profit_pct=0.0,
                outcome=outcome_type,
                open_time=entry.get("open_time", ""),
                close_time=close_time,
                duration_mins=round(dur_mins, 1),
            )
            memory.record(outcome)
            _stats = memory.stats(trading_type=trading_type, live_only=True)
            rl_manager.on_trade_closed(
                trading_type=trading_type,
                profit_pct=profit,
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
            "confidence":   0.5,
        }
        asyncio.create_task(
            _poll_outcome(ticket=entry["ticket"], signal=fake_signal, client=client)
        )
        logger.info(f"Recovery: re-launched poll for still-open #{entry['ticket']} {entry.get('symbol')}")


async def _poll_outcome(ticket: int, signal: dict, client) -> None:
    """
    Poll MT5 every 30 s until the position is closed, then record
    the outcome in TradeMemory and feed the result to the RL agent.

    Stops polling after 7 days (swing trade max hold time).
    """
    import MetaTrader5 as mt5
    from ai.trade_memory import memory, TradeOutcome
    from ai.rl_agent import rl_manager

    MAX_POLLS  = 7 * 24 * 120   # 7 days at 30 s intervals
    open_time  = datetime.now(tz=timezone.utc).isoformat()

    for _ in range(MAX_POLLS):
        await asyncio.sleep(30)
        try:
            # Position still open?
            positions = await asyncio.to_thread(mt5.positions_get, ticket=ticket)
            if positions:
                continue   # still open

            # Look in history by position ID — more reliable than time range
            deals = await asyncio.to_thread(mt5.history_deals_get, position=ticket)
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
            # Use a symbol-aware pip tolerance: 3 pips for forex, 300 points for BTC/XAUUSD
            _sym_upper = sym.upper()
            if any(x in _sym_upper for x in ("BTC", "XAU", "GOLD", "US30", "US100", "DAX", "UK100")):
                tol = abs(close_px) * 0.001   # 0.1% of price
            else:
                tol = pip_val * 3   # 3-pip tolerance for forex
            direction  = signal["direction"].upper()
            pips       = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_val
            # Use deal.time corrected for broker server-clock offset to store true UTC
            _srv_offset = await asyncio.to_thread(_get_server_utc_offset_secs, sym)
            close_time = datetime.fromtimestamp(deal.time - _srv_offset, tz=timezone.utc).isoformat()

            # Determine outcome type
            sl = float(signal.get("sl", 0))
            tp = float(signal.get("tp") or 0)
            if tp and abs(close_px - tp) <= tol:
                outcome_type = "tp_hit"
            elif sl and abs(close_px - sl) <= tol:
                outcome_type = "sl_hit"
            else:
                outcome_type = "manual_close"

            open_dt  = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            close_dt = datetime.fromtimestamp(deal.time, tz=timezone.utc)
            dur_mins = (close_dt - open_dt).total_seconds() / 60

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
                tp_price=tp,
                volume=float(signal.get("lot_size", 0.01)),
                profit=profit,
                profit_pips=round(pips, 1),
                profit_pct=0.0,   # balance not captured here — RL uses raw profit
                outcome=outcome_type,
                open_time=open_time,
                close_time=close_time,
                duration_mins=round(dur_mins, 1),
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
            stats = memory.stats(trading_type=trading_type, live_only=True)

            # Update consecutive win/loss counter in the risk manager
            _drawdown_pct = 0.0
            try:
                from api.runner_loop import _risk_manager as _rm
                if _rm is not None:
                    if profit > 0:
                        _rm.record_win(trading_type)
                    else:
                        _rm.record_loss(trading_type)
                    # Update drawdown tracking with current account balance
                    from api.main import get_mt5_client as _gclient2
                    _c2 = _gclient2()
                    if _c2 and _c2.is_connected():
                        _acct = await asyncio.to_thread(_c2.get_account_info)
                        if _acct and _acct.get("balance"):
                            _rm.update_balance(_acct["balance"])
                            # Compute current daily drawdown % for RL state
                            if _rm._day_start_balance and _rm._day_start_balance > 0:
                                _drawdown_pct = max(
                                    0.0,
                                    (_rm._day_start_balance - _acct["balance"])
                                    / _rm._day_start_balance * 100.0,
                                )
            except Exception:
                pass

            # Normalise reward: convert raw USD profit → % of account balance so
            # the RL reward is scale-invariant across account sizes and lot sizes.
            _profit_pct = profit
            try:
                from api.main import get_mt5_client as _gc
                _c = _gc()
                if _c and _c.is_connected():
                    _acct = await asyncio.to_thread(_c.get_account_info)
                    if _acct and _acct.get("balance", 0) > 0:
                        _profit_pct = profit / _acct["balance"] * 100.0
            except Exception:
                pass

            rl_manager.on_trade_closed(
                trading_type=trading_type,
                profit_pct=_profit_pct,
                win_rate=stats.get("win_rate", 0.5),
                avg_conf=stats.get("avg_conf", 0.5),
                drawdown_pct=_drawdown_pct,
            )
            logger.info(
                f"Outcome recorded: #{ticket} {signal['symbol']} {outcome_type} "
                f"profit={profit:+.2f} ({_profit_pct:+.4f}%) pips={pips:+.1f}"
            )

            # ── Auto LSTM retrain ─────────────────────────────────────────────
            # Trigger background retrain once we have enough trades (every 20th).
            # Guard against burst: track which keys were already triggered this poll
            # by checking whether a training job was just launched in this process
            # session for this key (predictor.is_training() covers that window).
            try:
                from ai.predictor import predictor, TRADING_TYPE_TF
                from ai.trade_memory import memory as _mem
                _sym   = signal["symbol"]
                _type  = trading_type
                _key   = f"{_sym}_{_type}"
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
                        _df = await asyncio.to_thread(_client.get_ohlcv, _sym, _tf_str, 1000)
                        if _df is not None and not _df.empty:
                            predictor.train_async(_sym, _df, _type)
                            logger.info(f"Auto LSTM retrain triggered [{_retrain_reason}]: {_key} ({_total} trades)")
            except Exception as _exc:
                logger.debug(f"Auto LSTM retrain skipped: {_exc}")

            # ── Auto param optimizer ──────────────────────────────────────────
            # Trigger when enough trades exist and win_rate suggests re-optimization
            try:
                from ai.param_optimizer import optimizer as _opt
                _strat = signal.get("strategy", "")
                _sym   = signal["symbol"]
                if _strat and _opt.should_reoptimize(_strat, _sym):
                    _tf_str2 = TRADING_TYPE_TF.get(trading_type, "H1")
                    from api.main import get_mt5_client as _gclient
                    _client2 = _gclient()
                    if _client2 and _client2.is_connected():
                        _df2 = await asyncio.to_thread(_client2.get_ohlcv, _sym, _tf_str2, 1500)
                        if _df2 is not None and not _df2.empty:
                            _opt.optimize_async(_strat, _sym, _df2, trading_type)
                            logger.info(f"Auto param optimizer triggered: {_strat}/{_sym}")
            except Exception as _exc2:
                logger.debug(f"Auto param optimizer skipped: {_exc2}")

            return

        except Exception as exc:
            logger.warning(f"_poll_outcome error for #{ticket}: {exc}")
            await asyncio.sleep(60)


# Application-level singleton — import this everywhere
bus = SignalBus()
