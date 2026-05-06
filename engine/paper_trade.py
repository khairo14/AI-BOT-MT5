"""
Paper Trade Engine
In paper mode every order is sent to the XM Demo account (login 168442709).
MT5Client already routes to demo when TRADING_MODE=paper — this layer adds:
  - paper trade labeling (comment prefix "paper|")
  - separate in-memory position ledger for dashboard display
  - trade history keyed separately from live history
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager, OrderRequest, OrderResult
from engine.risk_manager import RiskManager
from engine.strategy_runner import StrategyRunner, StrategySignal

# Module-level singleton — set by main.py at startup
paper_engine: "PaperTradeEngine | None" = None

# Cap on in-memory closed-trade history (FIFO eviction beyond this)
_HISTORY_MAX = 1_000


@dataclass
class PaperPosition:
    ticket: int
    symbol: str
    direction: str
    lot_size: float
    open_price: float
    sl_price: float
    tp_price: float
    open_time: float
    comment: str
    strategy: str
    trading_type: str
    current_price: float = 0.0
    profit: float = 0.0
    closed: bool = False
    close_price: float = 0.0
    close_time: float = 0.0
    confidence: float = 0.5  # signal confidence at entry time (for RL/analytics)


class PaperTradeEngine:
    """
    Thin wrapper around StrategyRunner that:
    1. Enforces demo-account routing (verified by MT5Client mode)
    2. Prefixes all order comments with "paper|"
    3. Maintains a local ledger of paper positions for the dashboard
    4. Provides a sync method to reconcile local ledger with MT5 positions
    """

    def __init__(
        self,
        client: MT5Client,
        order_manager: OrderManager,
        risk_manager: RiskManager,
        execution_mode: str = "manual",
    ):
        self.client        = client
        self.order_manager = order_manager
        self.risk_manager  = risk_manager
        self.runner        = StrategyRunner(client, order_manager, risk_manager, execution_mode)

        self._lock:      threading.Lock                = threading.Lock()
        self._positions: dict[int, PaperPosition]      = {}  # ticket → PaperPosition
        self._history:   list[PaperPosition]           = []

    # ── public API ────────────────────────────────────────────────────────────

    def scan_and_signal(self) -> list[StrategySignal]:
        """Run all strategies and return signals (auto-execute or queue, per mode)."""
        if not self._verify_paper_mode():
            logger.error("PaperTradeEngine: not in paper mode — refusing to run")
            return []
        return self.runner.run_all()

    def place_paper_order(self, sig: StrategySignal) -> PaperPosition | None:
        """Execute a signal on the demo account and record it in the local ledger."""
        if not self._verify_paper_mode():
            return None

        comment = f"paper|{sig.comment}"
        req = OrderRequest(
            symbol=sig.symbol,
            direction=sig.direction,
            volume=sig.lot_size,
            sl=sig.sl_price,
            tp=sig.tp_price,
            entry_price=sig.entry_price,   # enables SL/TP reanchor to live fill price
            comment=comment,
        )
        result: OrderResult = self.order_manager.place_market_order(req)
        if not result or not result.success:
            logger.warning(f"PaperTrade order failed: {sig.symbol} {sig.direction}")
            return None

        pos = PaperPosition(
            ticket=result.ticket or 0,
            symbol=sig.symbol,
            direction=sig.direction,
            lot_size=sig.lot_size,
            open_price=result.open_price or 0.0,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price or 0.0,
            open_time=time.time(),
            comment=comment,
            strategy=sig.strategy,
            trading_type=sig.trading_type,
            current_price=result.open_price or 0.0,
            confidence=float(sig.confidence) if sig.confidence is not None else 0.5,
        )
        with self._lock:
            self._positions[result.ticket or 0] = pos
        logger.info(
            f"Paper position opened: {sig.symbol} {sig.direction} "
            f"lot={sig.lot_size} ticket={result.ticket}"
        )
        return pos

    def sync_positions(self) -> None:
        """Sync local ledger with actual MT5 demo positions and update P&L."""
        mt5_positions = self.client.get_open_positions()
        # PAPER-1: None means MT5 is disconnected — do not falsely close all positions.
        if mt5_positions is None:
            logger.debug("PaperTrade sync: MT5 returned None (disconnected) — skipping sync")
            return
        mt5_tickets   = {p["ticket"] for p in mt5_positions}

        with self._lock:
            snapshot = list(self._positions.items())

        for ticket, pos in snapshot:
            if pos.closed:
                continue
            mt5_match = next((p for p in (mt5_positions or []) if p["ticket"] == ticket), None)
            if mt5_match:
                pos.current_price = mt5_match.get("price_current", pos.current_price)
                pos.profit        = mt5_match.get("profit", 0.0)
                # Sync SL/TP in case _poll_outcome moved them (breakeven, trail)
                _live_sl = mt5_match.get("sl")
                _live_tp = mt5_match.get("tp")
                if _live_sl and _live_sl != pos.sl_price:
                    pos.sl_price = float(_live_sl)
                if _live_tp and _live_tp != pos.tp_price:
                    pos.tp_price = float(_live_tp)
            else:
                # Position no longer in MT5 — mark as closed.
                # H-7 fix: query deal history to get the actual fill price rather than
                # using pos.current_price which may be up to 60 s stale.
                # NEW-11 fix: use self.client.get_deals_by_position() which acquires
                # MT5Client._lock, preventing concurrent MT5 SDK calls.
                actual_close = None
                try:
                    import MetaTrader5 as _mt5
                    deals = self.client.get_deals_by_position(ticket)
                    if deals:
                        close_deals = [d for d in deals if d.entry == _mt5.DEAL_ENTRY_OUT]
                        if close_deals:
                            # Blend multiple partial exits (e.g. TP1 + SL) by volume
                            # so close_price reflects the true average, not just the last deal.
                            _total_vol = sum(getattr(d, 'volume', 0) for d in close_deals)
                            if _total_vol > 0:
                                actual_close = sum(
                                    getattr(d, 'price', 0) * getattr(d, 'volume', 0)
                                    for d in close_deals
                                ) / _total_vol
                            else:
                                actual_close = close_deals[-1].price
                except Exception:
                    pass
                pos.closed      = True
                pos.close_time  = time.time()
                pos.close_price = actual_close if actual_close is not None else pos.current_price
                with self._lock:
                    self._history.append(pos)
                    if len(self._history) > _HISTORY_MAX:
                        self._history = self._history[-_HISTORY_MAX:]
                try:
                    from engine.trade_journal import trade_journal
                    from engine.account_store import current_mode, current_account_login
                    from datetime import datetime, timezone
                    trade_journal.log(
                        ticket=ticket,
                        symbol=pos.symbol,
                        direction=pos.direction,
                        volume=pos.lot_size,
                        entry=pos.open_price,
                        sl=pos.sl_price,
                        tp=pos.tp_price if pos.tp_price else None,
                        profit=pos.profit,
                        trading_type=pos.trading_type,
                        account_mode=current_mode(),
                        comment=pos.comment,
                        event="close",
                        close_time=datetime.fromtimestamp(
                            pos.close_time, tz=timezone.utc
                        ).isoformat(),
                        account_login=current_account_login(),
                    )
                except Exception as _je:
                    logger.warning(f"Journal write failed for paper #{ticket}: {_je}")
                logger.info(
                    f"Paper position closed: {pos.symbol} {pos.direction} "
                    f"ticket={ticket} profit={pos.profit:.2f}"
                )
                try:
                    from engine.notification_manager import notification_manager as _nm_paper
                    from api.websocket.feed import manager as _ws_manager_paper
                    import asyncio as _asyncio_paper
                    _paper_sev = "success" if pos.profit > 0 else ("error" if pos.profit < 0 else "info")
                    _nm_paper.add(
                        type="position_closed",
                        title=f"{'✅ TP Hit' if pos.profit > 0 else '❌ SL Hit'} — {pos.symbol} (paper)",
                        message=(
                            f"{pos.direction} {pos.symbol} #{ticket} paper closed | "
                            f"P&L: {pos.profit:+.2f}"
                        ),
                        severity=_paper_sev,
                        metadata={"ticket": ticket, "symbol": pos.symbol, "profit": pos.profit, "paper": True},
                    )
                    _asyncio_paper.create_task(_ws_manager_paper.broadcast_alert({
                        "type": "position_closed",
                        "symbol": pos.symbol,
                        "profit": round(pos.profit, 2),
                        "pips": 0.0,
                        "outcome": "tp_hit" if pos.profit > 0 else "sl_hit",
                        "strategy": pos.strategy or "",
                    }))
                except Exception:
                    pass
                # Record to TradeMemory (feeds LSTM retrains, RL stats, analytics).
                # This was missing — paper closes only wrote to the journal, so
                # trade_memory and RL were only updated on server-restart recovery.
                try:
                    from ai.trade_memory import memory as _mem_pt, TradeOutcome as _TO
                    from datetime import datetime, timezone as _tz
                    _open_iso  = datetime.fromtimestamp(pos.open_time,  tz=_tz.utc).isoformat()
                    _close_iso = datetime.fromtimestamp(pos.close_time, tz=_tz.utc).isoformat()
                    _dur_mins  = (pos.close_time - pos.open_time) / 60.0
                    # Determine outcome by comparing actual close price to SL/TP.
                    # Using profit sign alone is unreliable — pos.profit is the last
                    # floating P&L from the previous sync cycle, which may be stale
                    # (e.g. price briefly went positive then reversed and hit SL).
                    _pip_val_pt = 0.01 if "JPY" in (pos.symbol or "") else 0.0001
                    # Tight tolerance: 0.005% of close price OR 1 pip — whichever is larger.
                    # Prior value (0.01% / 2 pips) was too loose for high-priced assets
                    # (GOLD $2000 → $0.20 tolerance, could misclassify outcome near TP/SL).
                    _tol_pt = max(abs(pos.close_price) * 0.00005, _pip_val_pt)
                    _dir_up = pos.direction.upper() == "BUY"
                    if _dir_up:
                        if pos.tp_price and pos.close_price >= pos.tp_price - _tol_pt:
                            _outcome_t = "tp_hit"
                        elif pos.sl_price and pos.close_price <= pos.sl_price + _tol_pt:
                            _outcome_t = "sl_hit"
                        else:
                            _outcome_t = "tp_hit" if pos.profit > 0 else "sl_hit"
                    else:
                        if pos.tp_price and pos.close_price <= pos.tp_price + _tol_pt:
                            _outcome_t = "tp_hit"
                        elif pos.sl_price and pos.close_price >= pos.sl_price - _tol_pt:
                            _outcome_t = "sl_hit"
                        else:
                            _outcome_t = "tp_hit" if pos.profit > 0 else "sl_hit"
                    _bal_pt    = 0.0
                    try:
                        _bal_pt = self.risk_manager._day_start_balance or 0.0
                    except Exception:
                        pass
                    _pnl_pct   = (pos.profit / _bal_pt * 100.0) if _bal_pt > 0 else (pos.profit / 10000.0 * 100.0)
                    _entry_px  = pos.open_price or 1e-8
                    _pips      = (pos.close_price - _entry_px) * (1 if pos.direction.upper() == "BUY" else -1) * 10000
                    _mem_pt.record(_TO(
                        ticket=ticket,
                        symbol=pos.symbol,
                        strategy=pos.strategy,
                        trading_type=pos.trading_type,
                        direction=pos.direction,
                        confidence=pos.confidence,
                        entry_price=pos.open_price,
                        close_price=pos.close_price,
                        sl_price=pos.sl_price,
                        tp_price=pos.tp_price,
                        volume=pos.lot_size,
                        profit=pos.profit,
                        profit_pips=round(_pips, 1),
                        profit_pct=_pnl_pct,
                        outcome=_outcome_t,
                        open_time=_open_iso,
                        close_time=_close_iso,
                        duration_mins=round(_dur_mins, 1),
                        mode="paper",
                        extra={"source": "paper"},
                    ))
                    # Notify RL agent using the freshly-updated stats
                    _stats_pt = _mem_pt.stats(trading_type=pos.trading_type, live_only=True)
                    _vol_pct  = abs(pos.open_price - pos.sl_price) / max(abs(pos.open_price), 1e-8) * 100.0 if pos.sl_price else 0.0
                    from ai.rl_agent import rl_manager as _rl_pt
                    _rl_pt.on_trade_closed(
                        strategy_name=pos.strategy,
                        trading_type=pos.trading_type,
                        profit_pct=_pnl_pct,
                        win_rate=_stats_pt.get("win_rate", 0.5),
                        avg_conf=_stats_pt.get("avg_conf", 0.5),
                        vol_pct=_vol_pct,
                    )
                except Exception as _rl_err:
                    logger.warning(f"TradeMemory/RL update skipped for paper #{ticket}: {_rl_err}")

    def get_open_positions(self) -> list[dict]:
        with self._lock:
            return [asdict(p) for p in self._positions.values() if not p.closed]

    def get_history(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return [asdict(p) for p in self._history[-limit:]]

    def get_stats(self) -> dict[str, Any]:
        closed = self._history
        if not closed:
            return {"total_trades": 0, "win_rate": 0.0, "total_pnl": 0.0}
        wins   = [p for p in closed if p.profit > 0]
        losses = [p for p in closed if p.profit <= 0]
        return {
            "total_trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(closed) * 100, 1),
            "total_pnl": round(sum(p.profit for p in closed), 2),
            "avg_win":  round(sum(p.profit for p in wins)   / max(len(wins), 1), 2),
            "avg_loss": round(sum(p.profit for p in losses) / max(len(losses), 1), 2),
        }

    def pending_signals(self) -> list[StrategySignal]:
        return self.runner.pending_signals

    def confirm_signal(self, index: int) -> bool:
        """Confirm and execute a pending signal (manual mode)."""
        if index >= len(self.runner.pending_signals):
            return False
        sig = self.runner.pending_signals[index]
        pos = self.place_paper_order(sig)
        if pos:
            self.runner.pending_signals.pop(index)
            return True
        return False

    def reject_signal(self, index: int) -> None:
        self.runner.reject_signal(index)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _verify_paper_mode(self) -> bool:
        return self.client.trading_mode == "paper"
