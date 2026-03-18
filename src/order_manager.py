"""
order_manager.py – Places, modifies, and closes MT5 orders.
"""

from __future__ import annotations

from typing import Optional

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.logger import get_logger
from src.strategy import SIGNAL_BUY, SIGNAL_SELL

logger = get_logger("order_manager")


class OrderManager:
    """Handles order placement, modification, and closure via MT5."""

    def __init__(
        self,
        symbol: str,
        magic_number: int = 20240101,
        comment: str = "AI-BOT-MT5",
        deviation: int = 20,
    ) -> None:
        self.symbol = symbol
        self.magic_number = magic_number
        self.comment = comment
        self.deviation = deviation  # Maximum price deviation in points

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    def place_order(
        self,
        signal: int,
        lot_size: float,
        stop_loss: float,
        take_profit: float,
        price: Optional[float] = None,
    ) -> Optional[int]:
        """Submit a market order in the direction of *signal*.

        Args:
            signal: SIGNAL_BUY (1) or SIGNAL_SELL (-1).
            lot_size: Position size in lots.
            stop_loss: Absolute stop-loss price.
            take_profit: Absolute take-profit price.
            price: Limit/stop price; None for a market order.

        Returns:
            MT5 deal ticket on success, None on failure.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.error("MetaTrader5 package unavailable; cannot place order.")
            return None

        if signal not in (SIGNAL_BUY, SIGNAL_SELL):
            logger.warning("place_order called with invalid signal %s; skipping.", signal)
            return None

        order_type = mt5.ORDER_TYPE_BUY if signal == SIGNAL_BUY else mt5.ORDER_TYPE_SELL  # type: ignore[union-attr]

        tick = mt5.symbol_info_tick(self.symbol)  # type: ignore[union-attr]
        if tick is None:
            logger.error("Cannot retrieve tick for %s.", self.symbol)
            return None

        exec_price = (tick.ask if signal == SIGNAL_BUY else tick.bid) if price is None else price

        request: dict = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore[union-attr]
            "symbol": self.symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": self.comment,
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[union-attr]
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore[union-attr]
        }

        result = mt5.order_send(request)  # type: ignore[union-attr]
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore[union-attr]
            logger.error(
                "Order failed | Symbol: %s | Signal: %s | retcode: %s | comment: %s",
                self.symbol,
                "BUY" if signal == SIGNAL_BUY else "SELL",
                result.retcode if result else "N/A",
                result.comment if result else "N/A",
            )
            return None

        logger.info(
            "Order placed | Symbol: %s | %s | Lots: %.2f | Price: %.5f | SL: %.5f | TP: %.5f | Ticket: %d",
            self.symbol,
            "BUY" if signal == SIGNAL_BUY else "SELL",
            lot_size,
            exec_price,
            stop_loss,
            take_profit,
            result.deal,
        )
        return int(result.deal)

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def get_open_positions(self) -> list:
        """Return all open positions for the configured symbol and magic number."""
        if not MT5_AVAILABLE or mt5 is None:
            return []
        positions = mt5.positions_get(symbol=self.symbol)  # type: ignore[union-attr]
        if positions is None:
            return []
        return [p for p in positions if p.magic == self.magic_number]

    def close_position(self, position) -> bool:
        """Close an open position.

        Args:
            position: An MT5 TradePosition named tuple.

        Returns:
            True if the close order was accepted, False otherwise.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.error("MetaTrader5 package unavailable; cannot close position.")
            return False

        tick = mt5.symbol_info_tick(self.symbol)  # type: ignore[union-attr]
        if tick is None:
            logger.error("Cannot retrieve tick to close position %d.", position.ticket)
            return False

        # Reverse the original trade direction
        close_type = (
            mt5.ORDER_TYPE_SELL if position.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY  # type: ignore[union-attr]
        )
        close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask  # type: ignore[union-attr]

        request = {
            "action": mt5.TRADE_ACTION_DEAL,  # type: ignore[union-attr]
            "symbol": self.symbol,
            "volume": position.volume,
            "type": close_type,
            "position": position.ticket,
            "price": close_price,
            "deviation": self.deviation,
            "magic": self.magic_number,
            "comment": f"close #{position.ticket}",
            "type_time": mt5.ORDER_TIME_GTC,  # type: ignore[union-attr]
            "type_filling": mt5.ORDER_FILLING_IOC,  # type: ignore[union-attr]
        }

        result = mt5.order_send(request)  # type: ignore[union-attr]
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:  # type: ignore[union-attr]
            logger.error(
                "Failed to close position %d: retcode=%s comment=%s",
                position.ticket,
                result.retcode if result else "N/A",
                result.comment if result else "N/A",
            )
            return False

        logger.info("Closed position %d | Profit: %.2f", position.ticket, position.profit)
        return True

    def close_all_positions(self) -> int:
        """Close all open positions managed by this bot.

        Returns:
            Number of positions successfully closed.
        """
        positions = self.get_open_positions()
        closed = 0
        for pos in positions:
            if self.close_position(pos):
                closed += 1
        if positions:
            logger.info("Closed %d / %d positions.", closed, len(positions))
        return closed
