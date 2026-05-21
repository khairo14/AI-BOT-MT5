"""
Trailing Stop Manager — automatically move SL in profit direction.

Monitors open positions every tick and adjusts stop-loss when price moves
favorably, locking in profit while allowing positions to run.

Key Features:
- Activation threshold: only trail after X pips profit (R-factor or fixed pips)
- Trail distance: maintain Y pips from current price (R-factor or fixed pips)
- Never moves SL against profit (only upward for BUY, downward for SELL)
- Per-mode configuration (scalping/day_trading/swing)
- Symbol-level overrides for activation/trail distances
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

# TTL cache for app.json — same 5-second pattern used by order_manager and signal_bus.
# Ensures trailing stop config changes made via the dashboard take effect within
# one scan cycle without requiring a bot restart.
import time as _ts_time
import threading as _ts_threading
_ts_cfg_cache: dict = {}
_ts_cfg_loaded_at: float = 0.0
_TS_CFG_TTL = 5.0
_ts_cfg_lock = _ts_threading.Lock()


def _get_ts_app_cfg() -> dict:
    """Return app.json contents, re-reading from disk at most once every 5 s."""
    global _ts_cfg_cache, _ts_cfg_loaded_at
    now = _ts_time.monotonic()
    if now - _ts_cfg_loaded_at < _TS_CFG_TTL:
        return _ts_cfg_cache
    with _ts_cfg_lock:
        if now - _ts_cfg_loaded_at < _TS_CFG_TTL:
            return _ts_cfg_cache
        try:
            import json as _json
            _ts_cfg_cache = _json.loads(
                (CONFIG_DIR / "app.json").read_text(encoding="utf-8")
            )
        except Exception:
            pass
        _ts_cfg_loaded_at = now
    return _ts_cfg_cache


@dataclass
class TrailingState:
    """Track trailing stop state for a position."""
    ticket: int
    symbol: str
    direction: str  # "BUY" or "SELL"
    entry_price: float
    current_sl: float
    highest_profit_price: float  # BUY: highest bid seen, SELL: lowest ask seen
    last_trail_at: Optional[datetime] = None
    pips_trailed: float = 0.0
    consecutive_failures: int = 0  # suppress retry spam after repeated modify errors


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
        Get the pip size for a symbol from MT5 symbol info.
        Falls back to forex defaults if symbol info is unavailable.
        - Forex non-JPY: 0.0001
        - Forex JPY: 0.01
        - Commodities (BRENT, OIL, GOLD, SILVER): 0.01
        - Indices (US30, US100): 1.0
        - Crypto (BTCUSD): 1.0 (price unit)
        """
        sym = symbol.upper()
        
        # Crypto: use price unit (1 point = $1)
        if any(x in sym for x in ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE")):
            return 1.0
        
        # Indices: use index point (1 point = 1 index unit)
        if any(x in sym for x in ("US30", "US100", "US500", "GER40", "UK100", "FRA40")):
            return 1.0
        
        # Commodities: typically 0.01 (gold, oil, etc.)
        if any(x in sym for x in ("GOLD", "XAUUSD", "SILVER", "XAGUSD", "OIL", "BRENT", "NGAS")):
            return 0.01
        
        try:
            info = self._client.get_symbol_info(symbol)
            if info and info.get("point"):
                point = info["point"]
                digits = info.get("digits", 5)
                # On odd-digit symbols (5-decimal forex, 3-decimal JPY) one pip
                # is 10 MT5 points. On even-digit symbols the pip == point.
                if digits in (5, 3):
                    return point * 10
                return point
        except Exception:
            pass
        
        # Fallback: classify by symbol name
        if "JPY" in sym:
            return 0.01
        return 0.0001

    def _extract_mode_from_comment(self, comment: str) -> Optional[str]:
        """Extract trading mode from position comment."""
        comment_lower = comment.lower()
        prefix = comment_lower.split("|")[0] if "|" in comment_lower else comment_lower
        if prefix == "scalp":
            return "scalping"
        if prefix in ("day", "day_trading"):
            return "day_trading"
        if prefix == "swing":
            return "swing"
        return None

    def _is_market_tradable(self, symbol: str) -> bool:
        """
        Check if the market is currently tradable for this symbol.
        Crypto and indices trade 24/7. Forex and commodities have weekend gaps.
        """
        sym = symbol.upper()
        _now = datetime.now(timezone.utc)
        _dow = _now.weekday()  # 5=Saturday, 6=Sunday
        
        # Crypto and indices trade 24/7 — always tradable
        crypto = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"}
        indices = {"US30", "US100", "US500", "GER40", "UK100", "FRA40"}
        if any(x in sym for x in crypto) or any(x in sym for x in indices):
            return True
        
        # Forex and commodities: weekend gap
        # Sunday: wait until 21:00 UTC (Sydney open)
        if _dow == 6:  # Sunday
            return _now.hour >= 21
        # Saturday: all day closed
        if _dow == 5:  # Saturday
            return False
        
        return True

    def update_trailing_stops(self) -> int:
        """
        Check all open positions and update trailing stops.
        Returns number of positions trailed.
        """
        _ts_cfg = _get_ts_app_cfg().get("trailing_stops", self._config)
        if not _ts_cfg.get("enabled", False):
            return 0

        positions = self._client.get_open_positions()
        if not positions:
            return 0

        trailed_count = 0

        for pos in positions:
            try:
                ticket = pos["ticket"]
                symbol = pos["symbol"]
                direction = "BUY" if pos["type"] == "buy" else "SELL"
                entry_price = pos["open_price"]
                current_sl = pos["sl"]
                comment = pos.get("comment", "")

                # Skip if market is closed for this symbol type
                if not self._is_market_tradable(symbol):
                    continue

                # Extract mode from comment
                mode = self._extract_mode_from_comment(comment)
                if not mode:
                    continue

                # Skip if trailing disabled for this mode
                mode_cfg = _ts_cfg.get(mode, {})
                if not mode_cfg.get("enabled", False):
                    continue

                # Get current price
                price_data = self._client.get_current_price(symbol)
                if not price_data:
                    continue

                current_price = price_data["bid"] if direction == "BUY" else price_data["ask"]
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

                # Update highest profit price (best price seen since open)
                if direction == "BUY":
                    if current_price > state.highest_profit_price:
                        state.highest_profit_price = current_price
                else:  # SELL
                    if current_price < state.highest_profit_price:
                        state.highest_profit_price = current_price

                # Symbol-level overrides — escape hatch for truly exceptional setups
                sym_override = mode_cfg.get("symbol_overrides", {}).get(symbol, {})

                # Calculate SL distance in price units (not pips)
                _sl_dist = abs(entry_price - current_sl) if current_sl > 0 else 0.0
                
                # If SL is 0 (no stop set), use a percentage-based fallback
                if _sl_dist == 0:
                    _sl_dist = entry_price * 0.01  # 1% fallback
                    logger.debug(f"TrailingStop: SL=0 for #{ticket}, using 1% fallback")

                # ── Activation distance (when to start trailing) ────────────────────
                # Priority: 1. activation_price (fixed pips), 2. activation_r_factor (dynamic)
                if "activation_price" in sym_override:
                    activation_distance = float(sym_override["activation_price"]) * pip_value
                elif _sl_dist > 0:
                    a_r = float(sym_override.get(
                        "activation_r_factor",
                        mode_cfg.get("activation_r_factor", 0.5)
                    ))
                    activation_distance = _sl_dist * a_r
                else:
                    # Fallback: 0.5% of entry price
                    activation_distance = entry_price * 0.005

                # ── Trail distance (how far SL follows) ─────────────────────────────
                # Priority: 1. trail_price (fixed pips), 2. trail_r_factor (dynamic)
                if "trail_price" in sym_override:
                    trail_distance = float(sym_override["trail_price"]) * pip_value
                elif _sl_dist > 0:
                    t_r = float(sym_override.get(
                        "trail_r_factor",
                        mode_cfg.get("trail_r_factor", 0.35)
                    ))
                    trail_distance = _sl_dist * t_r
                else:
                    # Fallback: 0.3% of entry price
                    trail_distance = entry_price * 0.003

                # Enforce broker minimum stop distance so modify_position never
                # silently fails on stocks/CFDs with a hard stops_level minimum.
                try:
                    _sym_info_ts = self._client.get_symbol_info(symbol)
                    if _sym_info_ts:
                        _sl_lvl = _sym_info_ts.get("stops_level", 0)
                        _pt = _sym_info_ts.get("point", 0.00001)
                        _sp = _sym_info_ts.get("spread", 0)
                        _broker_min = (_sl_lvl * _pt) if _sl_lvl > 0 else (_sp * _pt * 2)
                        if _broker_min > 0 and trail_distance < _broker_min * 1.2:
                            trail_distance = _broker_min * 1.2
                except Exception:
                    pass

                # Profit measured in price units (direction-aware)
                if direction == "BUY":
                    profit_distance = current_price - entry_price
                else:
                    profit_distance = entry_price - current_price

                # Check activation threshold
                if profit_distance < activation_distance:
                    continue

                # Calculate new SL
                if direction == "BUY":
                    new_sl = state.highest_profit_price - trail_distance
                else:
                    new_sl = state.highest_profit_price + trail_distance

                # Only move SL in profit direction (never backwards)
                should_update = False
                if direction == "BUY":
                    should_update = current_sl <= 0 or new_sl > current_sl
                else:
                    should_update = current_sl <= 0 or new_sl < current_sl

                if should_update:
                    # Back off for 12 ticks (~60 s) after 3 consecutive failures
                    if state.consecutive_failures >= 3:
                        state.consecutive_failures -= 1
                        continue
                    
                    # Modify position
                    success = self._om.modify_position(ticket, sl=new_sl, tp=pos["tp"])
                    if success:
                        state.consecutive_failures = 0
                        pips_moved = abs(new_sl - current_sl) / pip_value if pip_value > 0 else 0
                        state.current_sl = new_sl
                        state.last_trail_at = datetime.now(tz=timezone.utc)
                        state.pips_trailed += pips_moved
                        trailed_count += 1
                        logger.info(
                            f"TrailingStop: #{ticket} {symbol} {direction} | "
                            f"Moved SL {current_sl:.5f} → {new_sl:.5f} "
                            f"({pips_moved:.1f} pips) | Total trailed: {state.pips_trailed:.1f} pips"
                        )
                    else:
                        state.consecutive_failures = min(state.consecutive_failures + 1, 15)

            except Exception as exc:
                logger.warning(f"TrailingStop: error updating {pos.get('ticket', '?')}: {exc}")
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
            result: list[dict] = []
            for ticket in self._position_states:
                status = self.get_trailing_status(ticket)
                if status is not None:
                    result.append(status)
            return result