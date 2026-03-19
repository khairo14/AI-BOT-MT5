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
_TERMINAL_STATUSES = frozenset({"executed", "rejected", "failed"})

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
        """Remove terminal-state signals older than _SIGNAL_TTL_SECONDS. Returns count removed."""
        now = datetime.now(tz=timezone.utc)
        to_delete = []
        for sid, sig in self.queue.items():
            if sig.get("status") not in _TERMINAL_STATUSES:
                continue
            try:
                created = datetime.fromisoformat(sig["created_at"].replace("Z", "+00:00"))
                age = (now - created).total_seconds()
                if age > _SIGNAL_TTL_SECONDS:
                    to_delete.append(sid)
            except Exception:
                to_delete.append(sid)
        for sid in to_delete:
            self.queue.pop(sid, None)
        if to_delete:
            logger.debug(f"SignalBus: purged {len(to_delete)} stale signals")
        return len(to_delete)

    # ── public API ──────────────────────────────────────────────────────────

    async def add_signal(self, signal: dict) -> dict:
        """
        Accept a new signal from the runner loop or HTTP POST /signals/.
        Auto-execution fires immediately if configured; otherwise queues for
        manual approval and pushes to the dashboard signal queue via WebSocket.
        """
        from api.websocket.feed import broadcast_signal

        self.queue[signal["id"]] = signal
        # Lazily purge stale terminal-state signals to keep queue bounded
        self.purge_stale()

        exec_mode = self._get_exec_mode(signal.get("trading_mode", ""))
        if exec_mode == "auto" and self._order_manager is not None:
            signal["status"] = "executing"
            # Broadcast immediately so the dashboard card appears before execution
            asyncio.create_task(broadcast_signal(dict(signal)))
            asyncio.create_task(self._execute_async(signal))
        else:
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

        # Concurrent + per-symbol limit check at execution time
        try:
            from engine.risk_manager import RiskManager
            from engine.mt5_client import MT5Client
            if isinstance(self._client, MT5Client):
                open_positions = self._client.get_open_positions()
                # Import the shared risk manager instance via the runner loop
                from api.runner_loop import _risk_manager
                if _risk_manager is not None:
                    allowed, reason = _risk_manager.check_concurrent_limit(
                        signal.get("trading_mode", "day_trading"),
                        open_positions,
                        symbol=signal.get("symbol"),
                    )
                    if not allowed:
                        signal["rejection_reason"] = reason
                        logger.info(f"SignalBus blocked execution: {reason}")
                        return False
        except Exception as _exc:
            logger.debug(f"SignalBus: concurrent limit check skipped: {_exc}")

        try:
            trading_mode = signal.get("trading_mode", "day_trading")
            mode_prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(trading_mode, "bot")
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
                logger.warning(f"Signal execution failed: {signal['symbol']} {signal['direction']}")
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


# ── outcome poller ────────────────────────────────────────────────────────────

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

            # Look in history — query from just before trade open to now
            open_dt = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            end_dt  = datetime.now(tz=timezone.utc)
            deals = await asyncio.to_thread(
                mt5.history_deals_get, open_dt, end_dt
            )
            if deals is None:
                deals = []

            closed = [d for d in deals if d.position_id == ticket and d.entry == mt5.DEAL_ENTRY_OUT]
            if not closed:
                continue

            deal       = closed[-1]
            close_time = datetime.fromtimestamp(deal.time, tz=timezone.utc).isoformat()
            profit     = deal.profit
            close_px   = deal.price
            entry_px   = float(signal.get("fill_price") or signal.get("entry_price", 0))
            pip_val    = 0.0001 if "JPY" not in signal["symbol"] else 0.01
            direction  = signal["direction"].upper()
            pips       = ((close_px - entry_px) if direction == "BUY" else (entry_px - close_px)) / pip_val

            # Determine outcome type
            sl = float(signal.get("sl", 0))
            tp = float(signal.get("tp") or 0)
            tol = pip_val * 3   # 3-pip tolerance
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
            except Exception:
                pass

            trading_type = signal.get("trading_mode", "day_trading")
            stats = memory.stats(trading_type=trading_type)

            # Update consecutive win/loss counter in the risk manager
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
            except Exception:
                pass

            rl_manager.on_trade_closed(
                trading_type=trading_type,
                profit_pct=profit,
                win_rate=stats.get("win_rate", 0.5),
                avg_conf=stats.get("avg_conf", 0.5),
            )
            logger.info(
                f"Outcome recorded: #{ticket} {signal['symbol']} {outcome_type} "
                f"profit={profit:+.2f} pips={pips:+.1f}"
            )

            # ── Auto LSTM retrain ─────────────────────────────────────────────
            # Trigger background retrain once we have enough trades (every 20th)
            try:
                from ai.predictor import predictor, TRADING_TYPE_TF
                from ai.trade_memory import memory as _mem
                _sym   = signal["symbol"]
                _type  = trading_type
                _key   = f"{_sym}_{_type}"
                _total = len([
                    o for o in _mem.recent(n=500)
                    if o.get("symbol") == _sym and o.get("trading_type") == _type
                ])
                _meta  = predictor.status().get(_key, {})
                _last  = _meta.get("trained_at", "")
                _retrain = (
                    not predictor.is_training(_sym, _type) and
                    _total > 0 and _total % 20 == 0
                )
                if _retrain:
                    import MetaTrader5 as _mt5
                    _TF_MAP = {"M5": _mt5.TIMEFRAME_M5, "H1": _mt5.TIMEFRAME_H1, "H4": _mt5.TIMEFRAME_H4}
                    _tf_str = TRADING_TYPE_TF.get(_type, "H1")
                    _tf_mt5 = _TF_MAP.get(_tf_str, _mt5.TIMEFRAME_H1)
                    from api.main import get_mt5_client
                    _client = get_mt5_client()
                    if _client and _client.is_connected():
                        _df = await asyncio.to_thread(_client.get_ohlcv, _sym, _tf_mt5, 1000)
                        if _df is not None and not _df.empty:
                            predictor.train_async(_sym, _df, _type)
                            logger.info(f"Auto LSTM retrain triggered: {_key} ({_total} trades)")
            except Exception as _exc:
                logger.debug(f"Auto LSTM retrain skipped: {_exc}")

            # ── Auto param optimizer ──────────────────────────────────────────
            # Trigger when enough trades exist and win_rate suggests re-optimization
            try:
                from ai.param_optimizer import optimizer as _opt
                _strat = signal.get("strategy", "")
                _sym   = signal["symbol"]
                if _strat and _opt.should_reoptimize(_strat, _sym):
                    import MetaTrader5 as _mt5b
                    _TF_MAP2 = {"M5": _mt5b.TIMEFRAME_M5, "H1": _mt5b.TIMEFRAME_H1, "H4": _mt5b.TIMEFRAME_H4}
                    _tf_str2 = TRADING_TYPE_TF.get(trading_type, "H1")
                    _tf_mt52 = _TF_MAP2.get(_tf_str2, _mt5b.TIMEFRAME_H1)
                    from api.main import get_mt5_client as _gclient
                    _client2 = _gclient()
                    if _client2 and _client2.is_connected():
                        _df2 = await asyncio.to_thread(_client2.get_ohlcv, _sym, _tf_mt52, 1500)
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
