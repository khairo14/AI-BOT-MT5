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

CREDIT_RISK_UTILIZATION = 0.75

def effective_risk_capital(balance: float, credit: float = 0.0) -> float:
    balance = max(float(balance or 0.0), 0.0)
    credit = max(float(credit or 0.0), 0.0)
    return balance + (credit * CREDIT_RISK_UTILIZATION)

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

def _normalize_account_mode(value: str | None) -> str:
    mode = str(value or "live").lower().strip()
    if mode in ("paper", "demo", "test"):
        return "demo"
    return "live"

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

def _mt5_positions_get(**kwargs):
    fn = getattr(mt5, "positions_get", None)
    return fn(**kwargs) if fn else None


def _mt5_symbol_info_tick(symbol: str):
    fn = getattr(mt5, "symbol_info_tick", None)
    return fn(str(symbol)) if fn else None


def _mt5_symbol_info(symbol: str):
    fn = getattr(mt5, "symbol_info", None)
    return fn(str(symbol)) if fn else None


def _mt5_order_send(request: dict):
    fn = getattr(mt5, "order_send", None)
    return fn(request) if fn else None


def _mt5_history_deals_get(*, ticket: int):
    fn = getattr(mt5, "history_deals_get", None)
    return fn(ticket=ticket) if fn else None

def _mt5_last_error() -> str:
    fn = getattr(mt5, "last_error", None)
    return str(fn()) if fn else "unknown MT5 error"

def _mt5_order_calc_profit(
    order_type: int,
    symbol: str,
    volume: float,
    price_open: float,
    price_close: float,
):
    fn = getattr(mt5, "order_calc_profit", None)
    if fn is None:
        return None

    return fn(
        order_type,
        str(symbol),
        float(volume),
        float(price_open),
        float(price_close),
    )
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
    sl: Optional[float] = None
    tp: Optional[float] = None
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
    # Helper: derive trading type from comment
    # ------------------------------------------------------------------
    @staticmethod
    def _derive_trading_type_from_comment(comment: str) -> str:
        """Extract trading type from position comment."""
        comment_lower = comment.lower()
        if "scalp" in comment_lower:
            return "scalping"
        if "swing" in comment_lower:
            return "swing"
        if "day" in comment_lower:
            return "day_trading"
        return ""

    # ------------------------------------------------------------------
    # Place Order
    # ------------------------------------------------------------------
    def _final_risk_audit(
        self,
        *,
        symbol: str,
        direction: str,
        volume: float,
        entry_price: float,
        sl_price: float,
    ) -> tuple[bool, str]:
        """
        Final safety check before MT5 order_send.

        Uses MT5 order_calc_profit to estimate worst-case loss at SL.
        This avoids relying on fragile tick-value assumptions.
        """
        try:
            acct = self._client.get_account_info()
            if not acct:
                return False, "No account info available for final risk audit"

            balance = float(acct.get("balance") or 0)
            credit = float(acct.get("credit") or 0)
            risk_capital = effective_risk_capital(balance, credit)

            if risk_capital <= 0:
                return False, "Invalid effective risk capital for final risk audit"

            order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

            with self._client._lock:
                pnl_at_sl = _mt5_order_calc_profit(
                    order_type,
                    symbol,
                    volume,
                    entry_price,
                    sl_price,
                )

            if pnl_at_sl is None:
                return False, f"MT5 order_calc_profit failed: {_mt5_last_error()}"

            money_risk = abs(float(pnl_at_sl))
            actual_risk_pct = (money_risk / risk_capital) * 100.0

            try:
                from api.runner_loop import _risk_manager
                max_risk_pct = float(
                    getattr(_risk_manager, "_config", {}).get("max_risk_per_trade_pct", 1.5)
                )
            except Exception:
                max_risk_pct = 1.5

            if actual_risk_pct > max_risk_pct + 1e-9:
                return False, (
                    f"Final risk audit failed | {symbol} {direction} | "
                    f"entry={entry_price} sl={sl_price} volume={volume} | "
                    f"risk=${money_risk:.2f} / effective_capital=${risk_capital:.2f} "
                    f"(balance=${balance:.2f}, credit=${credit:.2f}, credit_used=75%) "
                    f"({actual_risk_pct:.2f}% > max {max_risk_pct:.2f}%)"
                )

            return True, (
                f"Final risk audit OK | {symbol} {direction} | "
                f"entry={entry_price} sl={sl_price} volume={volume} | "
                f"risk=${money_risk:.2f} / effective_capital=${risk_capital:.2f} "
                f"(balance=${balance:.2f}, credit=${credit:.2f}, credit_used=75%) "
                f"({actual_risk_pct:.2f}% <= max {max_risk_pct:.2f}%)"
            )

        except Exception as exc:
            return False, f"Final risk audit exception: {exc}"

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
            tick = _mt5_symbol_info_tick(req.symbol)
        if tick is None:
            return OrderResult(success=False, error=f"No tick data for {req.symbol}")

        with self._client._lock:
            sym_info = _mt5_symbol_info(req.symbol)
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
                # BUY: MT5 fires SL when bid <= sl, TP when bid >= tp.
                # Anchor to bid so stops trigger at the strategy-intended price.
                sl = round(tick.bid - sl_dist, 6)
                if tp is not None:
                    tp_dist = abs(req.entry_price - tp)
                    tp = round(tick.bid + tp_dist, 6)
            else:  # SELL
                # SELL: MT5 fires SL when ask >= sl, TP when ask <= tp.
                # Anchor to ask so stops trigger at the strategy-intended price.
                sl = round(tick.ask + sl_dist, 6)
                if tp is not None:
                    tp_dist = abs(req.entry_price - tp)
                    tp = round(tick.ask - tp_dist, 6)
            if sl != req.sl or tp != req.tp:
                logger.debug(
                    f"SL/TP reanchored: entry={req.entry_price} bid={tick.bid:.5f} ask={tick.ask:.5f} "
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
            # stops_level=0: broker enforces no fixed minimum, but MT5 still
            # rejects if SL lands exactly on the bid/ask (SL = ask - spread = bid
            # for a BUY is still "Invalid stops" on most brokers).  Add 1 pip of
            # safety margin above the raw spread so SL is strictly inside the book.
            _pip_pts = 10 if sym_info.digits in (3, 5) else 1
            min_dist = (sym_info.spread + _pip_pts) * sym_info.point
        if min_dist > 0:
            # Use bid as reference for BUY stops, ask for SELL stops — matches
            # MT5's closure rules: BUY SL/TP compared against bid, SELL against ask.
            _ref = tick.bid if req.direction == "BUY" else tick.ask
            sl_dist = abs(_ref - sl)
            if sl_dist < min_dist:
                logger.warning(
                    f"SL too close for {req.symbol}: {sl_dist:.5f} < min {min_dist:.5f} "
                    f"(stops_level={stops_level}) — adjusting SL to minimum distance"
                )
                if req.direction == "BUY":
                    sl = round(_ref - min_dist, sym_info.digits)
                else:
                    sl = round(_ref + min_dist, sym_info.digits)
            # Also enforce minimum distance for TP — MT5 rejects the whole order if
            # either stop is inside the freeze/stops zone (common on crypto CFDs where
            # stops_level=0 but the broker still enforces a real server-side minimum).
            if tp is not None:
                tp_dist = abs(_ref - tp)
                if tp_dist < min_dist:
                    logger.warning(
                        f"TP too close for {req.symbol}: {tp_dist:.5f} < min {min_dist:.5f} "
                        f"(stops_level={stops_level}) — adjusting TP to minimum distance"
                    )
                    if req.direction == "BUY":
                        tp = round(_ref + min_dist, sym_info.digits)
                    else:
                        tp = round(_ref - min_dist, sym_info.digits)

                    # Revalidate reward:risk after live reanchor and broker stop-distance adjustment.
                    # Broker stop adjustment can widen SL or pull TP closer, so a signal that was
                    # valid at strategy time may no longer meet minimum RR at execution time.
        try:
            _mode_prefix_rr = req.comment.split("|")[0] if "|" in req.comment else ""
            _mode_map_rr = {"scalp": "scalping", "day": "day_trading", "swing": "swing"}
            _tt_rr = _mode_map_rr.get(_mode_prefix_rr, "")

            if tp is not None:
                risk_dist = abs(price - sl)
                reward_dist = abs(tp - price)

                if risk_dist <= 0:
                    return OrderResult(
                        success=False,
                        error="Adjusted RR invalid: risk distance is zero",
                    )

                adjusted_rr = reward_dist / risk_dist

                try:
                    from api.runner_loop import _risk_manager

                    _risk_cfg = getattr(_risk_manager, "_config", {}) if _risk_manager else {}
                    _by_mode = _risk_cfg.get("risk_reward_min_by_mode", {})
                    min_rr = float(
                        _by_mode.get(
                            _tt_rr,
                            _risk_cfg.get("risk_reward_min", 1.2),
                        )
                    )
                except Exception:
                    min_rr = 1.2

                if adjusted_rr < min_rr - 1e-9:
                    return OrderResult(
                        success=False,
                        error=(
                            f"Adjusted RR {adjusted_rr:.2f} below minimum {min_rr:.2f} "
                            f"after SL/TP reanchor [{_tt_rr or 'unknown'}]"
                        ),
                    )
        except Exception as _rr_exc:
            logger.debug(f"Adjusted RR validation skipped: {_rr_exc}")
        # Live spread gate — block entry if current spread exceeds mode limit.
        # Uses real-time spread from MT5 (not hardcoded) so news spikes are caught.
        _captured_spread_pips: Optional[float] = None
        try:
            _app_cfg = _get_om_app_cfg()
            _mode_prefix = req.comment.split("|")[0] if "|" in req.comment else ""
            _mode_map = {"scalp": "scalping", "day": "day_trading", "swing": "swing"}
            _tt = _mode_map.get(_mode_prefix, "")
            if _tt:
                _points_per_pip = 10 if sym_info.digits in (3, 5) else 1
                _current_sp = float(sym_info.spread) / _points_per_pip
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

        # Anti-slippage: read max deviation from config per trading mode.
        # Lower value = tighter entry — MT5 rejects fills beyond this many points.
        # On 5-digit forex: 5 pts = 0.5 pip (scalping), 10 pts = 1 pip (day),
        # 20 pts = 2 pips (swing). Exits (close_position) always use 20 to ensure fills.
        _mode_prefix_dev = req.comment.split("|")[0] if "|" in req.comment else ""
        _mode_map_dev = {"scalp": "scalping", "day": "day_trading", "swing": "swing"}
        _tt_dev = _mode_map_dev.get(_mode_prefix_dev, "")
        try:
            _dev_cfg = _get_om_app_cfg().get("max_deviation_points", {})
            _deviation = int(_dev_cfg.get(_tt_dev, 20)) if _tt_dev else 20
        except Exception:
            _deviation = 20
            
        audit_ok, audit_msg = self._final_risk_audit(
            symbol=req.symbol,
            direction=req.direction,
            volume=vol,
            entry_price=price,
            sl_price=sl,
        )

        if not audit_ok:
            original_vol = vol

            # Reduce volume step-by-step until final live risk audit passes.
            # Do NOT change SL/TP here; only reduce exposure.
            while step > 0:
                next_vol = round(math.floor((vol - step) / step) * step, 10)

                if next_vol < sym_info.volume_min:
                    break

                retry_ok, retry_msg = self._final_risk_audit(
                    symbol=req.symbol,
                    direction=req.direction,
                    volume=next_vol,
                    entry_price=price,
                    sl_price=sl,
                )

                if retry_ok:
                    logger.warning(
                        f"Final risk audit adjusted volume | {req.symbol} {req.direction} | "
                        f"{original_vol} -> {next_vol} | {retry_msg}"
                    )
                    vol = next_vol
                    audit_ok = True
                    audit_msg = retry_msg
                    break

                vol = next_vol
                audit_msg = retry_msg

            if not audit_ok:
                logger.warning(audit_msg)
                return OrderResult(success=False, error=audit_msg)

        logger.debug(audit_msg)

        request = {
            "action":    mt5.TRADE_ACTION_DEAL,
            "symbol":    req.symbol,
            "volume":    vol,
            "type":      order_type,
            "price":     price,
            "sl":        sl,
            "tp":        tp if tp else 0.0,
            "deviation": _deviation,
            "magic":     req.magic,
            "comment":   req.comment[:31],  # MT5 limit: 31 chars
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }

        with self._client._lock:
            result = _mt5_order_send(request)
        execution_time_ms = int(time.time() * 1000) - start_time_ms

        # Calculate slippage for display. Unit depends on instrument type so the
        # number is always meaningful:
        #   Forex 5/3-digit : pip  (0.0001 / 0.01)  — e.g. 1.5 pips on EURUSD
        #   Forex 4-digit   : point (0.0001)
        #   Stocks          : raw $ distance         — e.g. 0.67 on ON-Semi
        #   Crypto/Indices  : raw price distance     — e.g. 0.003 on XLMUSD
        # Using digits in (3,5) → pip=10×point as the ONLY formula caused stock
        # slippage to be reported as hundreds of "pips" (display bug).
        slippage = None
        if req.entry_price and result and result.retcode == mt5.TRADE_RETCODE_DONE:
            raw_slip = abs(result.price - req.entry_price)
            _sym_u = req.symbol.upper()
            if sym_info.digits in (5, 3):
                # Standard 5-digit forex (EURUSD) or 3-digit JPY (USDJPY)
                pip_size = sym_info.point * 10
            elif any(x in _sym_u for x in (
                "US30", "US100", "US500", "GER40", "UK100",
                "BTC", "ETH", "SOL", "XRP", "XLM", "BNB", "LTC", "ADA"
            )):
                # Indices and crypto — report raw price distance (no "pips")
                pip_size = 1.0
            elif sym_info.digits <= 2:
                # Stocks (2-decimal) — report raw $ distance, not pip fractions
                pip_size = 1.0
            else:
                pip_size = sym_info.point  # 4-digit forex, commodities
            slippage = round(raw_slip / pip_size, 2) if pip_size > 0 else raw_slip

        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(_mt5_last_error())
            logger.error(
                f"Order failed | {req.symbol} {req.direction} "
                f"{req.volume} lots | Error: {err}"
            )
            return OrderResult(success=False, error=err)

        confirmed_pos = self._confirm_open_position(
            symbol=req.symbol,
            direction=req.direction,
            magic=req.magic,
            comment=req.comment,
            result_order=getattr(result, "order", None),
        )

        if confirmed_pos is None:
            err = (
                f"Order retcode DONE but no confirmed MT5 position found "
                f"symbol={req.symbol} direction={req.direction} order={getattr(result, 'order', None)} "
                f"deal={getattr(result, 'deal', None)}"
            )
            logger.error(err)
            return OrderResult(success=False, error=err)

        position_ticket = int(confirmed_pos.ticket)
        open_price = float(confirmed_pos.price_open or result.price)
        executed_sl = float(getattr(confirmed_pos, "sl", 0.0) or sl or 0.0)
        executed_tp_raw = float(getattr(confirmed_pos, "tp", 0.0) or 0.0)
        executed_tp = executed_tp_raw if executed_tp_raw > 0 else tp

        logger.info(
            f"Order placed | #{position_ticket} | {req.symbol} {req.direction} "
            f"{vol} lots | Entry: {open_price} | SL: {executed_sl} | TP: {executed_tp} | "
            f"Execution: {execution_time_ms}ms"
            + (f" | Slippage: {slippage:.5f}" if slippage else "")
        )

        return OrderResult(
            success=True,
            ticket=position_ticket,
            open_price=open_price,
            sl=executed_sl,
            tp=executed_tp,
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
            positions = _mt5_positions_get(ticket=ticket)
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
            digits = sym_info.get("digits", 5)
            # Normalize sl/tp to symbol precision before validation and order send.
            # Floating-point tails (e.g. 0.249856789...) cause MT5 "Invalid stops".
            if new_sl > 0:
                new_sl = round(new_sl, digits)
            if new_tp > 0:
                new_tp = round(new_tp, digits)

            stops_level = sym_info.get("stops_level", 0)
            point = sym_info.get("point", 0.00001)
            if stops_level > 0:
                min_distance = stops_level * point
            else:
                # When stops_level=0 the broker still enforces a minimum equal to
                # the current spread.  symbol_info() returns spread in POINTS (not
                # price distance) so multiply by point to get actual price distance.
                # "spread_pips" does NOT exist in the MT5 symbol_info dict — use
                # sym_info["spread"] (raw spread in points) instead.
                spread_pts = sym_info.get("spread", 0)
                min_distance = spread_pts * point if spread_pts > 0 else 0.0

            # Get LIVE bid/ask from symbol_info_tick() — symbol_info() only has
            # static metadata (digits, point, spread, etc.), NOT live prices.
            bid = ask = None
            try:
                with self._client._lock:
                    _tick = _mt5_symbol_info_tick(pos.symbol)
                if _tick:
                    bid = _tick.bid
                    ask = _tick.ask
                    # stops_level=0: broker enforces live spread as minimum distance.
                    # sym_info["spread"] is stale cached metadata; use live ask-bid
                    # instead to match what MT5 will actually enforce at order_send().
                    if stops_level == 0 and bid > 0 and ask > 0:
                        live_spread = ask - bid
                        if live_spread > min_distance:
                            min_distance = live_spread
            except Exception:
                pass
            
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
            result = _mt5_order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(_mt5_last_error())
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
            positions = _mt5_positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"close_position: ticket #{ticket} not found")
            return False

        pos = positions[0]
        with self._client._lock:
            tick = _mt5_symbol_info_tick(pos.symbol)
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
            result = _mt5_order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(_mt5_last_error())
            logger.error(f"close_position #{ticket} failed: {err}")
            return False
        
        deal = self._get_deal_by_ticket(
            int(getattr(result, "deal", 0) or 0)
        )

        if deal is None:
            logger.error(
                f"close_position #{ticket}: MT5 returned DONE but close deal not found "
                f"deal={getattr(result, 'deal', None)} — journal skipped"
            )
            return True

        from datetime import datetime, timezone

        close_time = datetime.fromtimestamp(
            float(getattr(deal, "time", 0.0) or 0.0),
            tz=timezone.utc,
        ).isoformat()
        close_profit = float(getattr(deal, "profit", 0.0) or 0.0)
        close_swap = float(getattr(deal, "swap", 0.0) or 0.0)
        close_commission = float(getattr(deal, "commission", 0.0) or 0.0)

        logger.info(
            f"Position closed | #{ticket} | {pos.symbol} | "
            f"Reason: {reason} | Close price: {price}"
        )
        
        # Derive trading_type from position comment
        trading_type = self._derive_trading_type_from_comment(pos.comment)
        
        try:
            from engine.trade_journal import trade_journal
            from engine.account_store import current_mode, current_account_login
            trade_journal.log(
                ticket=ticket,
                symbol=pos.symbol,
                direction="BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=pos.volume,
                entry=pos.price_open,
                sl=pos.sl,
                tp=pos.tp if pos.tp else None,
                trading_type=trading_type,
                account_mode=_normalize_account_mode(current_mode()),
                comment=reason,
                strategy=pos.comment or reason,
                event="close",
                profit=close_profit,
                close_time=close_time,
                swap=close_swap,
                commission=close_commission,
                account_login=current_account_login(),
                account_type=_normalize_account_mode(current_mode()),
                user_id="default",
            )
        except Exception as _je:
            logger.debug(f"Journal write failed for close #{ticket}: {_je}")
    
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
            sym_info = _mt5_symbol_info(symbol)
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
            positions = _mt5_positions_get(ticket=ticket)
        if not positions:
            logger.warning(f"partial_close: ticket #{ticket} not found")
            return False

        pos = positions[0]
        with self._client._lock:
            symbol_info = _mt5_symbol_info(pos.symbol)
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
            tick = _mt5_symbol_info_tick(pos.symbol)
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
            result = _mt5_order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            err = result.comment if result else str(_mt5_last_error())
            logger.error(f"partial_close #{ticket} ({close_pct*100:.0f}%) failed: {err}")
            return False

        logger.info(
            f"Partial close | #{ticket} | {pos.symbol} | "
            f"{close_pct*100:.0f}% ({close_volume} lots) | Reason: {reason}"
        )
        
        # Derive trading_type from position comment
        trading_type = self._derive_trading_type_from_comment(pos.comment)
        
        # H-5 fix: journal the partial close so trailing audit logs are complete.
        # Fetch the real profit from the MT5 deal record using result.deal so the
        # journal, stats, RL agent, and win-rate all see the correct figure.
        try:
            from engine.trade_journal import trade_journal
            from engine.account_store import current_mode, current_account_login
            from datetime import datetime, timezone

            deal = self._get_deal_by_ticket(int(getattr(result, "deal", 0) or 0))

            if deal is None:
                logger.error(
                    f"partial_close #{ticket}: MT5 returned DONE but partial close deal not found "
                    f"deal={getattr(result, 'deal', None)} — journal skipped"
                )
                return True

            _partial_profit_raw = float(getattr(deal, "profit", 0.0) or 0.0)
            _partial_swap = float(getattr(deal, "swap", 0.0) or 0.0)
            _partial_commission = float(getattr(deal, "commission", 0.0) or 0.0)
            _partial_fee = float(getattr(deal, "fee", 0.0) or 0.0)

            _partial_profit = _partial_profit_raw + _partial_swap + _partial_commission + _partial_fee
            close_time = datetime.fromtimestamp(
                float(getattr(deal, "time", 0.0) or 0.0),
                tz=timezone.utc,
            ).isoformat()


            account_mode = _normalize_account_mode(current_mode())
            account_login = int(current_account_login() or 0)

            trade_journal.log(
                ticket=ticket,
                symbol=pos.symbol,
                direction="BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
                volume=close_volume,
                entry=pos.price_open,
                sl=pos.sl,
                tp=pos.tp if pos.tp else None,
                profit=_partial_profit,
                trading_type=trading_type,
                account_type=account_mode,
                user_id="default",
                strategy=reason,
                account_mode=account_mode,
                comment=reason,
                event="partial_close",
                close_time=close_time,
                swap=_partial_swap,
                commission=_partial_commission,
                account_login=account_login,
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
        An empty/unrecognized prefix falls back to matching any bot position on that symbol.
        """
        with self._client._lock:
            positions = _mt5_positions_get(symbol=symbol)
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
    
    def _get_deal_by_ticket(self, deal_ticket: int):
        if not deal_ticket:
            return None

        with self._client._lock:
            deals = _mt5_history_deals_get(ticket=deal_ticket)

        if deals:
            return deals[0]

        return None


    def _confirm_open_position(
        self,
        *,
        symbol: str,
        direction: str,
        magic: int,
        comment: str,
        result_order: int | None,
    ):
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

        # First try direct ticket lookup.
        if result_order:
            with self._client._lock:
                direct = _mt5_positions_get(ticket=result_order)
            if direct:
                return direct[0]

        # Fallback: find matching live position by broker facts.
        with self._client._lock:
            positions = _mt5_positions_get(symbol=symbol)

        if not positions:
            return None

        candidates = [
            p for p in positions
            if p.magic == magic
            and p.type == order_type
            and str(p.comment or "") == str(comment or "")[:31]
        ]

        if candidates:
            return sorted(candidates, key=lambda p: int(getattr(p, "time", 0) or 0), reverse=True)[0]

        return None