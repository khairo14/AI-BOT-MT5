"""
Paper Trade Engine
In paper mode every order is sent to the XM Demo account (login 168442709).
MT5Client already routes to demo when TRADING_MODE=paper — this layer adds:
  - paper trade labeling (comment prefix "paper|")
  - separate in-memory position ledger for dashboard display
  - trade history keyed separately from live history
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from loguru import logger

from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager, OrderRequest, OrderResult
from engine.risk_manager import RiskManager
from engine.strategy_runner import StrategyRunner, StrategySignal


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

        self._positions: dict[int, PaperPosition] = {}  # ticket → PaperPosition
        self._history:   list[PaperPosition]       = []

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
            lot_size=sig.lot_size,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
            comment=comment,
        )
        result: OrderResult = self.order_manager.place_market_order(req)
        if not result or not result.success:
            logger.warning(f"PaperTrade order failed: {sig.symbol} {sig.direction}")
            return None

        pos = PaperPosition(
            ticket=result.ticket,
            symbol=sig.symbol,
            direction=sig.direction,
            lot_size=sig.lot_size,
            open_price=result.price,
            sl_price=sig.sl_price,
            tp_price=sig.tp_price,
            open_time=time.time(),
            comment=comment,
            strategy=sig.strategy,
            trading_type=sig.trading_type,
            current_price=result.price,
        )
        self._positions[result.ticket] = pos
        logger.info(
            f"Paper position opened: {sig.symbol} {sig.direction} "
            f"lot={sig.lot_size} ticket={result.ticket}"
        )
        return pos

    def sync_positions(self) -> None:
        """Sync local ledger with actual MT5 demo positions and update P&L."""
        mt5_positions = self.client.get_open_positions()
        mt5_tickets   = {p["ticket"] for p in mt5_positions} if mt5_positions else set()

        for ticket, pos in list(self._positions.items()):
            if pos.closed:
                continue
            mt5_match = next((p for p in (mt5_positions or []) if p["ticket"] == ticket), None)
            if mt5_match:
                pos.current_price = mt5_match.get("price_current", pos.current_price)
                pos.profit        = mt5_match.get("profit", 0.0)
            else:
                # Position no longer in MT5 — mark as closed
                pos.closed     = True
                pos.close_time = time.time()
                price_info     = self.client.get_current_price(pos.symbol)
                pos.close_price = (
                    price_info.get("bid", pos.current_price)
                    if pos.direction == "BUY"
                    else price_info.get("ask", pos.current_price)
                ) if price_info else pos.current_price
                self._history.append(pos)
                logger.info(
                    f"Paper position closed: {pos.symbol} {pos.direction} "
                    f"ticket={ticket} profit={pos.profit:.2f}"
                )

    def get_open_positions(self) -> list[dict]:
        return [asdict(p) for p in self._positions.values() if not p.closed]

    def get_history(self, limit: int = 100) -> list[dict]:
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
        return self.client.mode == "paper"
