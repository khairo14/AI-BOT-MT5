"""
Order Manager — place, modify, and close trades via MT5.
All orders are validated against the risk manager before execution.
Scalping orders are delegated to the MQL5 EA via named pipe (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import MetaTrader5 as mt5
from loguru import logger

from engine.mt5_client import MT5Client

# Magic number — identifies all orders placed by this bot
BOT_MAGIC = 20260318


@dataclass
class OrderRequest:
    symbol: str
    direction: str        # "BUY" or "SELL"
    volume: float         # lot size (calculated by risk manager)
    sl: float             # stop-loss price
    tp: Optional[float]   # take-profit price (None = trailing only)
    comment: str = ""
    magic: int = BOT_MAGIC


@dataclass
class OrderResult:
    success: bool
    ticket: Optional[int] = None
    open_price: Optional[float] = None
    error: Optional[str] = None


class OrderManager:
    """
    Wraps MT5 order operations with validation, duplicate guard,
    and structured logging. Works for both paper (demo) and live accounts.
    """

    def __init__(self, client: MT5Client):
        self._client = client

    # ------------------------------------------------------------------
    # Place Order
    # ------------------------------------------------------------------

    def place_market_order(self, req: OrderRequest) -> OrderResult:
        """Place a market order (BUY or BUY_MARKET / SELL or SELL_MARKET)."""

        if not self._client.is_connected():
            return OrderResult(success=False, error="MT5 not connected")

        # Duplicate guard — reject if position already open in same symbol/direction
        if self._has_open_position(req.symbol, req.direction):
            return OrderResult(
                success=False,
                error=f"Duplicate rejected: {req.symbol} {req.direction} already open",
            )

        tick = mt5.symbol_info_tick(req.symbol)
        if tick is None:
            return OrderResult(success=False, error=f"No tick data for {req.symbol}")

        sym_info = mt5.symbol_info(req.symbol)
        if sym_info is None:
            return OrderResult(success=False, error=f"Symbol info unavailable for {req.symbol}")

        order_type = mt5.ORDER_TYPE_BUY if req.direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if req.direction == "BUY" else tick.bid

        # Validate SL is on the correct side of price
        if req.direction == "BUY" and req.sl >= price:
            return OrderResult(success=False, error="BUY SL must be below entry price")
        if req.direction == "SELL" and req.sl <= price:
            return OrderResult(success=False, error="SELL SL must be above entry price")

        # Enforce broker minimum stop distance (stops_level * point)
        stops_level = sym_info.trade_stops_level
        if stops_level > 0:
            min_dist = stops_level * sym_info.point
            sl_dist = abs(price - req.sl)
            if sl_dist < min_dist:
                return OrderResult(
                    success=False,
                    error=f"SL too close: {sl_dist:.5f} < broker minimum {min_dist:.5f} ({stops_level} points)",
                )

        # Use broker-supported filling mode (filling_mode bitmask: bit0=FOK, bit1=IOC)
        fm = sym_info.filling_mode
        if fm & 1:
            filling = mt5.ORDER_FILLING_FOK
        elif fm & 2:
            filling = mt5.ORDER_FILLING_IOC
        else:
            filling = mt5.ORDER_FILLING_RETURN

        # Clamp volume to broker-allowed range and round to volume_step
        vol = req.volume
        vol = max(sym_info.volume_min, min(vol, sym_info.volume_max))
        step = sym_info.volume_step
        if step > 0:
            import math
            vol = round(math.floor(vol / step) * step, 10)
            vol = max(sym_info.volume_min, vol)

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    req.symbol,
            "volume":    vol,
            "type":      order_type,
            "price":     price,
            "sl":        req.sl,
            "tp":        req.tp if req.tp else 0.0,
            "deviation": 20,       # max price slippage in points
            "magic":     req.magic,
            "comment":   req.comment[:31],  # MT5 limit: 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        result = mt5.order_send(request)

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(
                f"Order failed | {req.symbol} {req.direction} "
                f"{req.volume} lots | Error: {err}"
            )
            return OrderResult(success=False, error=err)

        logger.info(
            f"Order placed | #{result.order} | {req.symbol} {req.direction} "
            f"{req.volume} lots | Entry: {result.price} | SL: {req.sl} | TP: {req.tp}"
        )
        return OrderResult(
            success=True,
            ticket=result.order,
            open_price=result.price,
        )

    # ------------------------------------------------------------------
    # Modify SL / TP (e.g. trailing stop, move to breakeven)
    # ------------------------------------------------------------------

    def modify_position(
        self,
        ticket: int,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
    ) -> bool:
        """Move SL and/or TP on an existing open position."""

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"modify_position: ticket #{ticket} not found")
            return False

        pos = positions[0]
        new_sl = sl if sl is not None else pos.sl
        new_tp = tp if tp is not None else pos.tp

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   pos.symbol,
            "sl":       new_sl,
            "tp":       new_tp,
            "magic":    pos.magic,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"modify_position #{ticket} failed: {err}")
            return False

        logger.info(f"Position #{ticket} modified | SL: {new_sl} | TP: {new_tp}")
        return True

    # ------------------------------------------------------------------
    # Close Position
    # ------------------------------------------------------------------

    def close_position(self, ticket: int, reason: str = "manual") -> bool:
        """Close an open position by ticket number."""

        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"close_position: ticket #{ticket} not found")
            return False

        pos = positions[0]
        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            logger.error(f"close_position: no tick for {pos.symbol}")
            return False

        # Close type is the opposite of the open type
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     ticket,
            "symbol":       pos.symbol,
            "volume":       pos.volume,
            "type":         close_type,
            "price":        price,
            "deviation":    20,
            "magic":        pos.magic,
            "comment":      reason[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"close_position #{ticket} failed: {err}")
            return False

        logger.info(
            f"Position closed | #{ticket} | {pos.symbol} | "
            f"Reason: {reason} | Close price: {price}"
        )
        return True

    def close_all_positions(self, symbol: Optional[str] = None) -> int:
        """Close all open positions. Optionally filter by symbol. Returns count closed."""
        positions = self._client.get_open_positions(symbol=symbol)
        closed = 0
        for pos in positions:
            if pos["magic"] == BOT_MAGIC:
                if self.close_position(pos["ticket"], reason="close_all"):
                    closed += 1
        return closed

    # ------------------------------------------------------------------
    # Partial Close (for TP1 partial profit taking)
    # ------------------------------------------------------------------

    def partial_close(self, ticket: int, close_pct: float, reason: str = "tp1") -> bool:
        """
        Close a percentage of an open position.
        close_pct: 0.0–1.0 (e.g. 0.5 = close 50%)
        """
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"partial_close: ticket #{ticket} not found")
            return False

        pos = positions[0]
        symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            return False

        close_volume = round(pos.volume * close_pct, 2)
        # Enforce min lot constraint
        if close_volume < symbol_info.volume_min:
            close_volume = symbol_info.volume_min

        # Round to lot step
        step = symbol_info.volume_step
        close_volume = round(round(close_volume / step) * step, 8)

        tick = mt5.symbol_info_tick(pos.symbol)
        if tick is None:
            return False

        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "position":     ticket,
            "symbol":       pos.symbol,
            "volume":       close_volume,
            "type":         close_type,
            "price":        price,
            "deviation":    20,
            "magic":        pos.magic,
            "comment":      reason[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_RETURN,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"partial_close #{ticket} ({close_pct*100:.0f}%) failed: {err}")
            return False

        logger.info(
            f"Partial close | #{ticket} | {pos.symbol} | "
            f"{close_pct*100:.0f}% ({close_volume} lots) | Reason: {reason}"
        )
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_open_position(self, symbol: str, direction: str) -> bool:
        """Return True if a bot-placed position already exists for symbol+direction."""
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        return any(p.magic == BOT_MAGIC and p.type == order_type for p in positions)
