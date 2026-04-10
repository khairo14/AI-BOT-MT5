"""
Trailing Stop Manager — automatically move SL in profit direction.

Monitors open positions every tick and adjusts stop-loss when price moves
favorably, locking in profit while allowing positions to run.

Key Features:
- Activation threshold: only trail after X pips profit
- Trail distance: maintain Y pips from current price
- Never moves SL against profit (only upward for BUY, downward for SELL)
- Per-mode configuration (scalping/day_trading/swing)
- Thread-safe position tracking
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager

CONFIG_DIR = Path(__file__).parent.parent / "config"


@dataclass
class TrailingState:
    """Track trailing stop state for a position."""
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    current_sl: float
    highest_profit_price: float  # BUY: highest ask seen, SELL: lowest bid seen
    last_trail_at: Optional[datetime] = None
    pips_trailed: float = 0.0


class TrailingStopManager:
    """Manage trailing stops for all open positions."""

    def __init__(self, client: MT5Client, order_manager: OrderManager):
        self._client = client
        self._om = order_manager
        self._lock = threading.Lock()
        self._position_states: dict[int, TrailingState] = {}
        self._config = self._load_config()

    def _load_config(self) -> dict:
        """Load trailing stop configuration from app.json."""
        try:
            cfg = json.loads((CONFIG_DIR / "app.json").read_text(encoding="utf-8"))
            return cfg.get("trailing_stops", {})
        except Exception as exc:
            logger.warning(f"TrailingStop: config load failed: {exc}")
            return {"enabled": False}

    def _get_pip_value(self, symbol: str) -> float:
        """
        Get point value in pips for a symbol.
        JPY pairs: 1 pip = 0.01, others: 1 pip = 0.0001
        """
        if "JPY" in symbol.upper():
            return 0.01
        return 0.0001

    def _extract_mode_from_comment(self, comment: str) -> Optional[str]:
        """Extract trading mode from position comment."""
        comment_lower = comment.lower()
        if "scalping" in comment_lower:
            return "scalping"
        if "day_trading" in comment_lower or "day" in comment_lower:
            return "day_trading"
        if "swing" in comment_lower:
            return "swing"
        return None

    def update_trailing_stops(self) -> int:
        """
        Check all open positions and update trailing stops.
        Returns number of positions trailed.
        """
        if not self._config.get("enabled", False):
            return 0

        positions = self._client.get_open_positions()
        if not positions:
            return 0

        trailed_count = 0

        for pos in positions:
            try:
                ticket = pos["ticket"]
                symbol = pos["symbol"]
                direction = "BUY" if pos["type"] == 0 else "SELL"
                entry_price = pos["price_open"]
                current_sl = pos["sl"]
                comment = pos.get("comment", "")

                # Extract mode from comment
                mode = self._extract_mode_from_comment(comment)
                if not mode:
                    continue

                # Skip if trailing disabled for this mode
                # NOTE: Scalping uses EA (AIBotScalper.mq5) for sub-millisecond trailing
                mode_cfg = self._config.get(mode, {})
                if not mode_cfg.get("enabled", False):
                    continue

                # Get current price
                tick = self._client.get_tick(symbol)
                if not tick:
                    continue

                current_price = tick["ask"] if direction == "BUY" else tick["bid"]
                pip_value = self._get_pip_value(symbol)

                # Initialize state if new position
                if ticket not in self._position_states:
                    with self._lock:
                        self._position_states[ticket] = TrailingState(
                            ticket=ticket,
                            symbol=symbol,
                            direction=direction,
                            entry_price=entry_price,
                            current_sl=current_sl,
                            highest_profit_price=current_price,
                        )

                state = self._position_states[ticket]

                # Update highest profit price
                if direction == "BUY":
                    if current_price > state.highest_profit_price:
                        state.highest_profit_price = current_price
                else:  # SELL
                    if current_price < state.highest_profit_price:
                        state.highest_profit_price = current_price

                # Calculate profit in pips
                if direction == "BUY":
                    profit_pips = (current_price - entry_price) / pip_value
                else:
                    profit_pips = (entry_price - current_price) / pip_value

                # Check if we've reached activation threshold
                activation_pips = mode_cfg.get("activation_pips", 10)
                if profit_pips < activation_pips:
                    continue

                # Calculate new SL based on trail distance
                trail_distance_pips = mode_cfg.get("trail_distance_pips", 8)
                trail_distance = trail_distance_pips * pip_value

                if direction == "BUY":
                    new_sl = state.highest_profit_price - trail_distance
                else:
                    new_sl = state.highest_profit_price + trail_distance

                # Only move SL in profit direction (never backwards)
                should_update = False
                if direction == "BUY":
                    should_update = new_sl > current_sl and new_sl > state.current_sl
                else:
                    should_update = new_sl < current_sl and new_sl < state.current_sl

                if should_update:
                    # Modify position
                    success = self._om.modify_position(ticket, sl=new_sl, tp=pos["tp"])
                    if success:
                        pips_moved = abs(new_sl - current_sl) / pip_value
                        state.current_sl = new_sl
                        state.last_trail_at = datetime.now(tz=timezone.utc)
                        state.pips_trailed += pips_moved
                        trailed_count += 1
                        logger.info(
                            f"TrailingStop: #{ticket} {symbol} {direction} | "
                            f"Moved SL {current_sl:.5f} → {new_sl:.5f} "
                            f"({pips_moved:.1f} pips) | Total trailed: {state.pips_trailed:.1f} pips"
                        )

            except Exception as exc:
                logger.warning(f"TrailingStop: error updating #{ticket}: {exc}")
                continue

        # Clean up closed positions
        open_tickets = {p["ticket"] for p in positions}
        with self._lock:
            closed_tickets = [t for t in self._position_states if t not in open_tickets]
            for ticket in closed_tickets:
                del self._position_states[ticket]

        return trailed_count

    def get_trailing_status(self, ticket: int) -> Optional[dict]:
        """Get trailing stop status for a position."""
        state = self._position_states.get(ticket)
        if not state:
            return None

        return {
            "ticket": state.ticket,
            "symbol": state.symbol,
            "direction": state.direction,
            "entry_price": state.entry_price,
            "current_sl": state.current_sl,
            "highest_profit_price": state.highest_profit_price,
            "last_trail_at": state.last_trail_at.isoformat() if state.last_trail_at else None,
            "pips_trailed": round(state.pips_trailed, 1),
        }

    def get_all_trailing_status(self) -> list[dict]:
        """Get trailing status for all tracked positions."""
        with self._lock:
            return [
                self.get_trailing_status(ticket)
                for ticket in self._position_states
                if self.get_trailing_status(ticket) is not None
            ]
