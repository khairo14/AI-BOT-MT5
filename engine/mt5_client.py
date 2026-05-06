"""
MT5 Client — handles connection, account info, price data, and symbol info.
All interaction with the MetaTrader5 Python library goes through this module.
"""

import os
import json
import threading
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
from dotenv import load_dotenv
from loguru import logger

from engine.account_store import load_account

load_dotenv()

# ---------------------------------------------------------------------------
# Timeframe map: string → MT5 constant
# ---------------------------------------------------------------------------
TIMEFRAMES = {
    "M1":  mt5.TIMEFRAME_M1,
    "M2":  mt5.TIMEFRAME_M2,
    "M5":  mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1":  mt5.TIMEFRAME_H1,
    "H4":  mt5.TIMEFRAME_H4,
    "D1":  mt5.TIMEFRAME_D1,
    "W1":  mt5.TIMEFRAME_W1,
}


class MT5Client:
    """
    Manages the MT5 terminal connection and exposes account,
    symbol, and OHLCV data retrieval methods.
    """

    def __init__(self):
        self._connected = False
        self._lock = threading.RLock()  # RLock allows re-entrant acquisition (needed by OrderManager)
        # account_store takes priority over env var so dashboard switches survive restarts
            # Load credentials FIRST
        self._credentials = self._load_credentials()

        acc_type = self._credentials.get("type", "")
        self._trading_mode = "paper" if acc_type == "demo" else "live"


    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _load_credentials(self) -> dict:
        demo_accounts = json.loads(os.getenv("MT5_DEMO_ACCOUNTS", "[]"))
        live_accounts = json.loads(os.getenv("MT5_LIVE_ACCOUNTS", "[]"))
        all_accounts = demo_accounts + live_accounts
        
        # Get current account from account_mode.json (single source of truth)
        current_account = load_account()
        current_login = current_account.get("login", 0)

        for acc in all_accounts:
            if acc["login"] == current_login:
                return acc
        return all_accounts[0] if all_accounts else {}

    def connect(self) -> bool:
        """Initialize MT5 and log in. Returns True on success."""
        if not mt5.initialize():
            logger.error(f"MT5 initialize() failed: {mt5.last_error()}")
            return False

        creds = self._credentials
        authorized = mt5.login(
            login=creds["login"],
            password=creds["password"],
            server=creds["server"],
        )

        if not authorized:
            logger.error(
                f"MT5 login failed for account {creds['login']} "
                f"on {creds['server']}: {mt5.last_error()}"
            )
            mt5.shutdown()
            return False

        self._connected = True
        info = mt5.account_info()
        logger.info(
            f"Connected to MT5 | Login: {info.login} | "
            f"Type: {self._credentials.get('type', 'unknown')} | "
            f"Balance: {info.balance} {info.currency}"
        )
        return True

    def disconnect(self) -> None:
        """Shut down MT5 connection."""
        mt5.shutdown()
        self._connected = False
        logger.info("MT5 connection closed.")

    def is_connected(self) -> bool:
        """H-4 fix: protect mt5.terminal_info() call with _lock (not thread-safe)."""
        if not self._connected:
            return False
        with self._lock:
            return mt5.terminal_info() is not None

    def reconnect(self) -> bool:
        """Re-initialize and log in after a dropped connection."""
        if self.is_connected():
            return True
        logger.info("MT5 reconnecting...")
        try:
            mt5.shutdown()
        except Exception:
            pass
        if not mt5.initialize():
            logger.error(f"MT5 reconnect initialize failed: {mt5.last_error()}")
            return False
        creds = self._credentials
        authorized = mt5.login(
            login=creds["login"],
            password=creds["password"],
            server=creds["server"],
        )
        if not authorized:
            logger.error(f"MT5 reconnect login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False
        self._connected = True
        logger.info("MT5 reconnected successfully.")
        return True

    def switch_account(self, login: int) -> bool:
        """Switch to a specific MT5 account by login number."""
        self.disconnect()     
        
        # Reload credentials for the new account
        self._credentials = self._load_credentials()
        
        # Determine trading mode for backward compat
        acc_type = self._credentials.get("type", "")
        self._trading_mode = "paper" if acc_type == "demo" else "live"
        
        success = self.connect()
        if success:
            from engine.account_store import save_account
            save_account(login, acc_type)
        return success

    # ------------------------------------------------------------------
    # Account Info
    # ------------------------------------------------------------------

    def get_account_info(self) -> Optional[dict]:
        """Return account balance, equity, margin, and metadata."""
        if not self.is_connected():
            logger.warning("get_account_info called while not connected.")
            return None

        with self._lock:
            info = mt5.account_info()
        if info is None:
            logger.error(f"mt5.account_info() returned None: {mt5.last_error()}")
            return None

        return {
            "login":        info.login,
            "server":       info.server,
            "currency":     info.currency,
            "balance":      info.balance,
            "equity":       info.equity,
            "margin":       info.margin,
            "free_margin":  info.margin_free,
            "margin_level": info.margin_level,
            "profit":       info.profit,
            "leverage":     info.leverage,
            "mode":         self._trading_mode,
        }

    # ------------------------------------------------------------------
    # Symbol Info
    # ------------------------------------------------------------------

    def get_all_symbols(self) -> list:
        """Return all MT5 symbols via mt5.symbols_get() under the client lock.
        Using the client lock (not a caller-side lock) ensures mutual exclusion
        with every other MT5 call in the codebase."""
        with self._lock:
            result = mt5.symbols_get()
        if result is None:
            logger.warning(f"mt5.symbols_get() returned None: {mt5.last_error()}")
            return []
        return list(result)

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return tick size, pip value, spread, and trading constraints."""
        # M-11 fix: single lock acquisition covers all three MT5 calls, eliminating
        # the thread-interleave window between symbol_info, symbol_select, and symbol_info_tick.
        with self._lock:
            info = mt5.symbol_info(symbol)
            if info is None:
                logger.warning(f"Symbol not found: {symbol}")
                return None
            # Ensure symbol is visible in Market Watch
            if not info.visible:
                if not mt5.symbol_select(symbol, True):
                    logger.warning(f"symbol_select failed for {symbol}: {mt5.last_error()}")
            tick = mt5.symbol_info_tick(symbol)

        # Convert MT5 spread (in points) to pips — instrument-aware.
        # On 5-digit (0.00001) and 3-digit (0.001) brokers, 1 pip = 10 points.
        # On 4-digit (0.0001) / 2-digit (0.01) / indices / stocks, 1 point ≈ 1 pip/unit.
        # Without this, exotic pairs like USDSGD (2230 points) are mislabelled as
        # 0.0223 "pips" instead of 22.3 pips, bypassing the spread filter.
        _points_per_pip = 10 if info.digits in (5, 3) else 1
        spread_pips = round(info.spread / _points_per_pip, 2)

        return {
            "symbol":        info.name,
            "bid":           tick.bid if tick else None,
            "ask":           tick.ask if tick else None,
            "spread_pips":   spread_pips,
            "digits":        info.digits,
            "point":         info.point,
            "pip_value":     info.trade_tick_value,
            "tick_size":     info.trade_tick_size,
            "contract_size": info.trade_contract_size,
            "min_lot":       info.volume_min,
            "max_lot":       info.volume_max,
            "lot_step":      info.volume_step,
            "stops_level":   info.trade_stops_level,
            # MT5 SYMBOL_TRADE_MODE: 0=disabled, 1=longonly, 2=shortonly, 3=closeonly, 4=full
            "trade_mode":    info.trade_mode,
            "path":          info.path,
            "currency_base": info.currency_base,
            "currency_profit":info.currency_profit,
            "description":   info.description,
        }

    def get_current_price(self, symbol: str) -> Optional[dict]:
        """Return current bid/ask for a symbol."""
        with self._lock:
            tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {
            "symbol": symbol,
            "bid":    tick.bid,
            "ask":    tick.ask,
            "time":   datetime.fromtimestamp(tick.time, tz=timezone.utc),
        }

    # ------------------------------------------------------------------
    # OHLCV Data
    # ------------------------------------------------------------------

    def get_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        count: int = 500,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch the last `count` candles for a symbol/timeframe.
        Returns a DataFrame with columns: time, open, high, low, close, volume.
        """
        tf = TIMEFRAMES.get(timeframe.upper())
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        with self._lock:
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            logger.warning(
                f"No OHLCV data for {symbol} {timeframe}: {mt5.last_error()}"
            )
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]]
        df = df.sort_values(by="time").reset_index(drop=True)  # type: ignore[call-overload]
        return df

    def get_ohlcv_range(
        self,
        symbol: str,
        timeframe: str,
        date_from: datetime,
        date_to: datetime,
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV for a specific UTC date range."""
        tf = TIMEFRAMES.get(timeframe.upper())
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None

        with self._lock:
            rates = mt5.copy_rates_range(symbol, tf, date_from, date_to)
        if rates is None or len(rates) == 0:
            logger.warning(f"No range data for {symbol} {timeframe}: {mt5.last_error()}")
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.rename(columns={"tick_volume": "volume"})
        df = df[["time", "open", "high", "low", "close", "volume"]]
        df = df.sort_values(by="time").reset_index(drop=True)  # type: ignore[call-overload]
        return df

    # ------------------------------------------------------------------
    # Open Positions & History
    # ------------------------------------------------------------------

    def get_position_by_ticket(self, ticket: int):
        """Return the raw MT5 position object for a specific ticket, or None.
        Acquires MT5Client._lock — NEW-14 fix so _poll_outcome doesn't call the
        MT5 SDK directly and bypass the lock used by OrderManager/paper_trade.
        """
        with self._lock:
            positions = mt5.positions_get(ticket=ticket)
        return positions[0] if positions else None

    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Return all open positions, optionally filtered by symbol."""
        with self._lock:
            positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []

        return [
            {
                "ticket":      p.ticket,
                "symbol":        p.symbol,
                "type":          "buy" if p.type == mt5.ORDER_TYPE_BUY else "sell",
                "volume":        p.volume,
                "open_price":    p.price_open,
                "price_current": p.price_current,
                "sl":            p.sl,
                "tp":            p.tp,
                "profit":        p.profit,
                "swap":          p.swap,
                "open_time":     datetime.fromtimestamp(p.time, tz=timezone.utc),
                "comment":       p.comment,
                "magic":         p.magic,
            }
            for p in positions
        ]

    def get_trade_history(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> list[dict]:
        """Return closed trade history."""
        if date_from is None:
            date_from = datetime(2020, 1, 1, tzinfo=timezone.utc)
        if date_to is None:
            date_to = datetime.now(tz=timezone.utc)

        with self._lock:
            deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []

        return [
            {
                "ticket":    d.ticket,
                "order":     d.order,
                "symbol":    d.symbol,
                "type":      "buy" if d.type == mt5.DEAL_TYPE_BUY else "sell",
                "volume":    d.volume,
                "price":     d.price,
                "profit":    d.profit,
                "swap":      d.swap,
                "fee":       d.fee,
                "comment":   d.comment,
                "time":      datetime.fromtimestamp(d.time, tz=timezone.utc),
                "magic":     d.magic,
            }
            for d in deals
        ]

    def get_deals_by_position(self, position_ticket: int) -> list:
        """Return raw deal objects for a given position ticket (acquires SDK lock).

        Primary: position-based lookup (fast, no date range needed).
        Fallback: date-range search over the last 30 days filtered by position_id.
        Some brokers (e.g. XM demo) return nothing from the position-based call
        unless the corresponding history window has already been loaded locally —
        the date-range fallback guarantees we always find the deal.
        """
        from datetime import datetime, timedelta, timezone as _tz

        with self._lock:
            # FIRST: Try date-range lookup for recent trades (last 7 days)
            # This works more reliably on XM than position lookup
            to_dt = datetime.now(_tz.utc) + timedelta(hours=24)
            from_dt = to_dt - timedelta(days=9)
            recent_deals = mt5.history_deals_get(from_dt, to_dt)

            if recent_deals:
                for d in recent_deals:
                    entry_type = "IN" if d.entry == mt5.DEAL_ENTRY_IN else "OUT" if d.entry == mt5.DEAL_ENTRY_OUT else "UNKNOWN"
                    logger.info(f"  Deal: ticket={d.ticket}, pos_id={d.position_id}, order={d.order}, profit={d.profit}, entry={entry_type}")
                
                matches = [d for d in recent_deals if d.position_id == position_ticket or d.order == position_ticket]
                if matches:
                    for m in matches:
                        logger.info(f"MATCH: ticket={m.ticket}, profit={m.profit}, entry={'OUT' if m.entry == mt5.DEAL_ENTRY_OUT else 'IN'}")
                    return matches
            
            # SECOND: Try position-based lookup (works for older trades)
            deals = mt5.history_deals_get(position=position_ticket)
            if deals:
                return list(deals)
            
            # THIRD: Extended search (last 90 days)
            from_dt = to_dt - timedelta(days=90)
            all_deals = mt5.history_deals_get(from_dt, to_dt)
            if not all_deals:
                return []
            
            return [d for d in all_deals if d.position_id == position_ticket or d.order == position_ticket]

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
