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
        - Crypto (BTCUSD): 1.0

        NOTE: MT5 `info.point` is the smallest price increment, NOT a pip.
        On 5-digit forex (EURUSD, digits=5) and 3-digit JPY (USDJPY, digits=3),
        1 pip = 10 points. We normalise here so trail_distance_pips is always
        in real pips, not MT5 micro-points.
        """
        sym = symbol.upper()
        # Indices and crypto have no meaningful "pip" — use 1.0 (index point / $1)
        # regardless of what MT5 reports for digits/point on this broker.
        if any(x in sym for x in ("US30", "US100", "US500", "GER40", "UK100")):
            return 1.0
        if any(x in sym for x in ("BTC", "ETH", "SOL", "XRP")):
            return 1.0
        try:
            info = self._client.get_symbol_info(symbol)
            if info and info.get("point"):
                point  = info["point"]
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
        if any(x in sym for x in ("GOLD", "XAUUSD", "SILVER", "XAGUSD", "OIL", "BRENT", "NGAS")):
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

    def update_trailing_stops(self) -> int:
        """
        Check all open positions and update trailing stops.
        Returns number of positions trailed.
        """
        if not self._config.get("enabled", False):
            return 0

        # Skip entirely when markets are closed (weekends, session gaps).
        # MT5 rejects modify_position with "Market closed" — this prevents
        # thousands of error log entries over the weekend.
        from datetime import datetime, timezone
        _now = datetime.now(timezone.utc)
        _dow = _now.weekday()  # 5=Saturday, 6=Sunday
        if _dow == 6 or (_dow == 5 and _now.hour >= 21):
            # Saturday all day, or Friday after 21:00 UTC
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
                price_data = self._client.get_current_price(symbol)
                if not price_data:
                    continue

                current_price = price_data["ask"] if direction == "BUY" else price_data["bid"]
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

                # Symbol-level overrides — escape hatch for truly exceptional setups.
                # activation_price / trail_price still accepted if explicitly set.
                sym_override = mode_cfg.get("symbol_overrides", {}).get(symbol, {})

                # ── R-factor based distances (fully dynamic, per-trade SL) ────────
                # Activation and trail are expressed as fractions of this trade's
                # actual SL distance — not fixed pips, not % of price.  This works
                # for every symbol and every setup without any per-symbol config:
                #
                #   activation = sl_dist × activation_r_factor  (default 0.5)
                #     → "activate when profit = 50% of what was risked"
                #   trail      = sl_dist × trail_r_factor        (default 0.35 day / 0.4 swing)
                #     → "trail keeping SL at 35% of original risk from highest price"
                #
                # AUDCAD  SL=6.7 pips  → activation=3.35 pips  (fires before TP)
                # AMD     SL=$20       → trail=$7.00           (well above $2 broker min)
                # BTC     SL=$1 500    → trail=$525            (appropriate for crypto)
                # New symbol added to scanner? Works automatically. No config needed.
                _sl_dist = abs(entry_price - current_sl) if current_sl > 0 else 0.0

                _sym_info_ts = None
                try:
                    _sym_info_ts = self._client.get_symbol_info(symbol)
                except Exception:
                    pass

                # Activation distance
                if "activation_price" in sym_override:
                    activation_distance = float(sym_override["activation_price"])
                elif _sl_dist > 0:
                    a_r = float(sym_override.get(
                        "activation_r_factor",
                        mode_cfg.get("activation_r_factor", 0.5)
                    ))
                    activation_distance = _sl_dist * a_r
                else:
                    # SL is 0: safety fallback (should never reach normal trades)
                    activation_distance = entry_price * 0.005

                # Trail distance
                if "trail_price" in sym_override:
                    trail_distance = float(sym_override["trail_price"])
                elif _sl_dist > 0:
                    t_r = float(sym_override.get(
                        "trail_r_factor",
                        mode_cfg.get("trail_r_factor", 0.35)
                    ))
                    trail_distance = _sl_dist * t_r
                else:
                    trail_distance = entry_price * 0.003

                # Enforce broker minimum stop distance so modify_position never
                # silently fails on stocks/CFDs with a hard stops_level minimum.
                try:
                    if _sym_info_ts:
                        _sl_lvl     = _sym_info_ts.get("stops_level", 0)
                        _pt         = _sym_info_ts.get("point", 0.00001)
                        _sp         = _sym_info_ts.get("spread", 0)
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

                # GAP-2: skip positions where _poll_outcome's ATR trail has already
                # moved SL to breakeven or better — let that system own them.
                if current_sl > 0:
                    if direction == "BUY" and current_sl >= entry_price:
                        continue
                    if direction == "SELL" and current_sl <= entry_price:
                        continue

                # Check activation threshold
                if profit_distance < activation_distance:
                    continue

                if direction == "BUY":
                    new_sl = state.highest_profit_price - trail_distance
                else:
                    new_sl = state.highest_profit_price + trail_distance

                # Only move SL in profit direction (never backwards).
                # BUG-5 fix: use only current_sl (live MT5 value); state.current_sl
                # is stale because _poll_outcome can move SL without updating state.
                should_update = False
                if direction == "BUY":
                    should_update = new_sl > current_sl
                else:
                    should_update = new_sl < current_sl

                if should_update:
                    # Back off for 12 ticks (~60 s) after 3 consecutive failures
                    # to avoid spamming MT5 with the same rejected modification.
                    if state.consecutive_failures >= 3:
                        state.consecutive_failures -= 1  # countdown toward retry
                        continue
                    # Modify position
                    success = self._om.modify_position(ticket, sl=new_sl, tp=pos["tp"])
                    if success:
                        state.consecutive_failures = 0
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
                    else:
                        state.consecutive_failures = min(state.consecutive_failures + 1, 15)

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
