"""
Order Manager — place, modify, and close trades via MT5.
All orders are validated against the risk manager before execution.
Scalping orders are delegated to the MQL5 EA via named pipe (Phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import time

import MetaTrader5 as mt5
from loguru import logger

from engine.mt5_client import MT5Client
from engine.notification_manager import notification_manager

# IMPROVE-2: read from config/app.json at import time so the magic number is
# configurable without any source-code changes. Fallback keeps backward compat.
def _load_bot_magic() -> int:
    try:
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).parent.parent / "config" / "app.json").read_text(encoding="utf-8"))
        return int(cfg.get("bot_magic", 20260318))
    except Exception:
        return 20260318

BOT_MAGIC: int = _load_bot_magic()

# TTL cache for app.json — spread gate reads this on every order placement.
# Caching for 5 s avoids disk reads on high-frequency scalping runs while still
# picking up dashboard config changes (spread limits) within one scan cycle.
import time as _time
import threading as _threading
_om_cfg_cache: dict = {}
_om_cfg_loaded_at: float = 0.0
_OM_CFG_TTL = 5.0
_om_cfg_lock = _threading.Lock()

def _get_om_app_cfg() -> dict:
    global _om_cfg_cache, _om_cfg_loaded_at
    now = _time.monotonic()
    if now - _om_cfg_loaded_at < _OM_CFG_TTL:
        return _om_cfg_cache
    with _om_cfg_lock:
        if now - _om_cfg_loaded_at < _OM_CFG_TTL:
            return _om_cfg_cache
        try:
            from pathlib import Path as _Path
            import json as _json
            _om_cfg_cache = _json.loads(
                (_Path(__file__).parent.parent / "config" / "app.json")
                .read_text(encoding="utf-8")
            )
        except Exception:
            pass
        _om_cfg_loaded_at = now
    return _om_cfg_cache

@dataclass
class OrderRequest:
    symbol: str
    direction: str        # "BUY" or "SELL"
    volume: float         # lot size (calculated by risk manager)
    sl: float             # stop-loss price
    tp: Optional[float]   # take-profit price (None = trailing only)
    entry_price: Optional[float] = None  # signal-generation price; used to reanchor SL/TP to live tick
    comment: str = ""
    magic: int = BOT_MAGIC


@dataclass
class OrderResult:
    success: bool
    ticket: Optional[int] = None
    open_price: Optional[float] = None
    error: Optional[str] = None
    execution_time_ms: Optional[int] = None
    slippage: Optional[float] = None
    spread_pips: Optional[float] = None


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
        
        start_time_ms = int(time.time() * 1000)  # Track execution time

        if not self._client.is_connected():
            return OrderResult(success=False, error="MT5 not connected")

        # Duplicate guard — reject if position already open in same symbol/direction/mode
        if self._has_open_position(req.symbol, req.direction, req.comment):
            return OrderResult(
                success=False,
                error=f"Duplicate rejected: {req.symbol} {req.direction} already open",
            )

        # L-1 fix: acquire client lock for all raw MT5 calls so they don't race
        # with MT5Client's own lock-protected calls (tick feed, OHLCV fetches, etc.).
        with self._client._lock:
            tick = mt5.symbol_info_tick(req.symbol)
        if tick is None:
            return OrderResult(success=False, error=f"No tick data for {req.symbol}")

        with self._client._lock:
            sym_info = mt5.symbol_info(req.symbol)
        if sym_info is None:
            return OrderResult(success=False, error=f"Symbol info unavailable for {req.symbol}")

        order_type = mt5.ORDER_TYPE_BUY if req.direction == "BUY" else mt5.ORDER_TYPE_SELL
        price = tick.ask if req.direction == "BUY" else tick.bid

        # Reanchor SL/TP to live fill price when we have the signal generation price.
        # The strategy computes SL/TP from curr_close at bar time; by execution time
        # the live tick may have moved (especially for tight scalping stops), causing
        # "Invalid stops" broker rejections and immediate SL hits.
        sl = req.sl
        tp = req.tp
        if req.entry_price and req.entry_price != 0.0:
            sl_dist = abs(req.entry_price - sl)
            if req.direction == "BUY":
                sl = round(price - sl_dist, 6)
                if tp is not None:
                    tp_dist = abs(req.entry_price - tp)
                    tp = round(price + tp_dist, 6)
            else:  # SELL
                sl = round(price + sl_dist, 6)
                if tp is not None:
                    tp_dist = abs(req.entry_price - tp)
                    tp = round(price - tp_dist, 6)
            if sl != req.sl or tp != req.tp:
                logger.debug(
                    f"SL/TP reanchored to live price: entry={req.entry_price} → live={price:.5f} "
                    f"SL {req.sl:.5f}→{sl:.5f}  TP {req.tp}→{tp}"
                )

        # Validate SL is on the correct side of price
        if req.direction == "BUY" and sl >= price:
            return OrderResult(success=False, error="BUY SL must be below entry price")
        if req.direction == "SELL" and sl <= price:
            return OrderResult(success=False, error="SELL SL must be above entry price")

        # Validate TP is on the correct side of price
        if req.direction == "BUY" and tp is not None and tp <= price:
            return OrderResult(success=False, error="BUY TP must be above entry price")
        if req.direction == "SELL" and tp is not None and tp >= price:
            return OrderResult(success=False, error="SELL TP must be below entry price")

        # Enforce broker minimum stop distance.
        # When stops_level > 0, use it directly.  When the broker reports 0
        # (common for crypto / CFD instruments), the actual server-side minimum
        # equals the current spread — use that as a fallback so we never send
        # an order that will be rejected with "Invalid stops".
        stops_level = sym_info.trade_stops_level
        if stops_level > 0:
            min_dist = stops_level * sym_info.point
        else:
            min_dist = sym_info.spread * sym_info.point  # spread-based fallback
        if min_dist > 0:
            sl_dist = abs(price - sl)
            if sl_dist < min_dist:
                logger.warning(
                    f"SL too close for {req.symbol}: {sl_dist:.5f} < min {min_dist:.5f} "
                    f"(stops_level={stops_level}) — adjusting SL to minimum distance"
                )
                if req.direction == "BUY":
                    sl = round(price - min_dist, sym_info.digits)
                else:
                    sl = round(price + min_dist, sym_info.digits)
        # Live spread gate — block entry if current spread exceeds mode limit.
        # Uses real-time spread from MT5 (not hardcoded) so news spikes are caught.
        _captured_spread_pips: Optional[float] = None
        try:
            _app_cfg = _get_om_app_cfg()
            _mode_prefix = req.comment.split("|")[0] if "|" in req.comment else ""
            _mode_map = {"scalp": "scalping", "day": "day_trading", "swing": "swing"}
            _tt = _mode_map.get(_mode_prefix, "")
            if _tt:
                _current_sp = sym_info.spread * sym_info.point * (
                    10 if sym_info.digits in (3, 5) else 1
                )
                _captured_spread_pips = round(_current_sp, 2)
                _spread_limits = _app_cfg.get("max_spread_pips", {})
                _max_sp = _spread_limits.get(_tt)
                if _max_sp is not None and _current_sp > _max_sp:
                    return OrderResult(
                        success=False,
                        error=f"Spread too wide: {_current_sp:.2f} > max {_max_sp:.2f} pips [{_tt}]"
                    )
        except Exception as _sp_exc:
            logger.debug(f"Spread gate skipped: {_sp_exc}")

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
            "sl":        sl,
            "tp":        tp if tp else 0.0,
            "deviation": 20,       # max price slippage in points
            "magic":     req.magic,
            "comment":   req.comment[:31],  # MT5 limit: 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        with self._client._lock:
            result = mt5.order_send(request)
        execution_time_ms = int(time.time() * 1000) - start_time_ms

        # Calculate slippage in pips (if we have expected price from signal).
        # Divide by pip size so the value is instrument-agnostic:
        #   5-digit forex (EUR/USD): point=0.00001, digits=5 → pip = 0.0001
        #   3-digit JPY   (AUD/JPY): point=0.001,   digits=3 → pip = 0.01
        #   2-digit Gold  (XAU/USD): point=0.01,    digits=2 → pip = 0.01
        slippage = None
        if req.entry_price and result and result.retcode == mt5.TRADE_RETCODE_DONE:
            raw_slip = abs(result.price - req.entry_price)
            pip_size = sym_info.point * (10 if sym_info.digits in (3, 5) else 1)
            slippage = round(raw_slip / pip_size, 2) if pip_size > 0 else raw_slip

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(
                f"Order failed | {req.symbol} {req.direction} "
                f"{req.volume} lots | Error: {err}"
            )
            return OrderResult(success=False, error=err)

        logger.info(
            f"Order placed | #{result.order} | {req.symbol} {req.direction} "
            f"{req.volume} lots | Entry: {result.price} | SL: {sl} | TP: {tp} | "
            f"Execution: {execution_time_ms}ms"
            + (f" | Slippage: {slippage:.5f}" if slippage else "")
        )
        return OrderResult(
            success=True,
            ticket=result.order,
            open_price=result.price,
            execution_time_ms=execution_time_ms,
            slippage=slippage,
            spread_pips=_captured_spread_pips,
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

        with self._client._lock:
            positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"modify_position: ticket #{ticket} not found")
            return False

        pos = positions[0]
        new_sl = sl if sl is not None else pos.sl
        new_tp = tp if tp is not None else pos.tp

        # Validate stops against broker's minimum distance requirement.
        # Same spread-based fallback as place_market_order: when stops_level=0
        # the broker still enforces a minimum equal to the current spread.
        sym_info = self._client.get_symbol_info(pos.symbol)
        if sym_info:
            stops_level = sym_info.get("stops_level", 0)
            point = sym_info.get("point", 0.00001)
            if stops_level > 0:
                min_distance = stops_level * point
            else:
                spread = sym_info.get("spread", 0)
                min_distance = spread * point  # spread-based fallback
            
            # Get current price (BUY uses ASK to open, SELL uses BID to open)
            # For SL/TP validation, use the current quote
            bid = sym_info.get("bid")
            ask = sym_info.get("ask")
            
            if bid and ask and min_distance > 0:
                # For BUY positions: SL must be <= bid - min_distance, TP must be >= bid + min_distance
                # For SELL positions: SL must be >= ask + min_distance, TP must be <= ask - min_distance
                if pos.type == mt5.ORDER_TYPE_BUY:
                    # Check SL distance from bid
                    if new_sl > 0:
                        if new_sl > bid - min_distance:
                            logger.debug(
                                f"modify_position #{ticket}: BUY SL {new_sl:.5f} too close to bid {bid:.5f} | "
                                f"Min distance: {min_distance:.5f} | Skipped"
                            )
                            return False
                    # Check TP distance from bid  
                    if new_tp > 0:
                        if new_tp < bid + min_distance:
                            logger.debug(
                                f"modify_position #{ticket}: BUY TP {new_tp:.5f} too close to bid {bid:.5f} | "
                                f"Min distance: {min_distance:.5f} | Skipped"
                            )
                            return False
                else:  # SELL
                    # Check SL distance from ask
                    if new_sl > 0:
                        if new_sl < ask + min_distance:
                            logger.debug(
                                f"modify_position #{ticket}: SELL SL {new_sl:.5f} too close to ask {ask:.5f} | "
                                f"Min distance: {min_distance:.5f} | Skipped"
                            )
                            return False
                    # Check TP distance from ask
                    if new_tp > 0:
                        if new_tp > ask - min_distance:
                            logger.debug(
                                f"modify_position #{ticket}: SELL TP {new_tp:.5f} too close to ask {ask:.5f} | "
                                f"Min distance: {min_distance:.5f} | Skipped"
                            )
                            return False

        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "symbol":   pos.symbol,
            "sl":       new_sl,
            "tp":       new_tp,
            "magic":    pos.magic,
        }

        with self._client._lock:
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

        with self._client._lock:
            positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"close_position: ticket #{ticket} not found")
            return False

        pos = positions[0]
        with self._client._lock:
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
            "type_filling": self._filling_for(pos.symbol),
        }

        with self._client._lock:
            result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"close_position #{ticket} failed: {err}")
            return False

        logger.info(
            f"Position closed | #{ticket} | {pos.symbol} | "
            f"Reason: {reason} | Close price: {price}"
        )
        
        # Notify user of position closed
        notification_manager.add(
            type="position_closed",
            title="Position Closed",
            message=f"{pos.symbol} position #{ticket} closed — {reason}",
            severity="info",
            metadata={"ticket": ticket, "symbol": pos.symbol, "reason": reason, "close_price": price}
        )
        
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _filling_for(self, symbol: str) -> int:
        """Return the broker-supported filling mode for a symbol (same logic as place_market_order)."""
        with self._client._lock:
            sym_info = mt5.symbol_info(symbol)
        if sym_info is None:
            return mt5.ORDER_FILLING_RETURN
        fm = sym_info.filling_mode
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

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
        with self._client._lock:
            positions = mt5.positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"partial_close: ticket #{ticket} not found")
            return False

        pos = positions[0]
        with self._client._lock:
            symbol_info = mt5.symbol_info(pos.symbol)
        if symbol_info is None:
            return False

        close_volume = round(pos.volume * close_pct, 2)
        # Enforce min lot constraint
        if close_volume < symbol_info.volume_min:
            close_volume = symbol_info.volume_min

        # BUG-1: use floor (not round) so close_volume never exceeds the requested
        # fraction of the position (rounds up to nearest lot step = closes too much).
        import math as _math
        step = symbol_info.volume_step
        close_volume = round(_math.floor(close_volume / step) * step, 8)

        # Guard A: volume_min clamp may push close_volume above the actual remaining
        # position size (e.g. pos.volume=0.01 after a prior partial close, but
        # volume_min=0.02). Cap to pos.volume — this becomes a full close of the
        # residual, which is correct and safe.
        close_volume = min(close_volume, pos.volume)

        # Guard B: floor rounding could yield 0.0 when pos.volume < one lot step.
        # Sending a zero-volume order to MT5 returns INVALID_VOLUME; bail out cleanly.
        if close_volume <= 0:
            logger.warning(
                f"partial_close #{ticket}: computed close_volume=0 after rounding "
                f"(pos.volume={pos.volume}, close_pct={close_pct}, step={step}) — skipping"
            )
            return False

        with self._client._lock:
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
            "type_filling": self._filling_for(pos.symbol),
        }

        with self._client._lock:
            result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(mt5.last_error())
            logger.error(f"partial_close #{ticket} ({close_pct*100:.0f}%) failed: {err}")
            return False

        logger.info(
            f"Partial close | #{ticket} | {pos.symbol} | "
            f"{close_pct*100:.0f}% ({close_volume} lots) | Reason: {reason}"
        )
        # H-5 fix: journal the partial close so trailing audit logs are complete
        try:
            from engine.trade_journal import trade_journal
            from engine.account_store import current_mode
            from datetime import datetime, timezone
            trade_journal.log(
                ticket=ticket,
                symbol=pos.symbol,
                direction="BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=close_volume,
                entry=pos.price_open,
                sl=pos.sl,
                tp=pos.tp if pos.tp else None,
                profit= None,
                trading_type="",
                account_mode=current_mode(),
                comment=reason,
                event="partial_close",
                close_time=datetime.now(tz=timezone.utc).isoformat(),
            )
        except Exception as _je:
            logger.debug(f"partial_close journal write failed for #{ticket}: {_je}")
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _has_open_position(self, symbol: str, direction: str, comment: str = "") -> bool:
        """Return True if a bot-placed position already exists for symbol+direction+mode.
        Mode is derived from the comment prefix (scalp|, day|, swing|).
        An empty/unrecognised prefix falls back to matching any bot position on that symbol.
        """
        with self._client._lock:
            positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
        # Derive the mode prefix from the comment (e.g. "scalp|EMAScalp" → "scalp")
        mode_prefix = comment.split("|")[0] if "|" in comment else ""
        for p in positions:
            if p.magic != BOT_MAGIC or p.type != order_type:
                continue
            if mode_prefix:
                # Only block if same mode
                if p.comment.startswith(mode_prefix + "|") or p.comment == mode_prefix:
                    return True
            else:
                # No mode info — fall back to blocking any bot position on this symbol/dir
                return True
        return False
