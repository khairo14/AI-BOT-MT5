"""
MT5 Client — handles connection, account info, price data, and symbol info.
All interaction with the MetaTrader5 Python library goes through this module.
"""

import os
import json
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
from dotenv import load_dotenv
from loguru import logger

from engine.account_store import load_mode, save_mode

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
        # account_store takes priority over env var so dashboard switches survive restarts
        self._trading_mode = load_mode()
        self._credentials = self._load_credentials()

    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _load_credentials(self) -> dict:
        mode = self._trading_mode
        if mode == "live":
            return {
                "login":    int(os.getenv("MT5_LIVE_LOGIN", 0)),
                "password": os.getenv("MT5_LIVE_PASSWORD", ""),
                "server":   os.getenv("MT5_LIVE_SERVER", ""),
            }
        return {
            "login":    int(os.getenv("MT5_DEMO_LOGIN", 0)),
            "password": os.getenv("MT5_DEMO_PASSWORD", ""),
            "server":   os.getenv("MT5_DEMO_SERVER", ""),
        }

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
            f"Connected to MT5 | Mode: {self._trading_mode.upper()} | "
            f"Account: {info.login} | Server: {info.server} | "
            f"Balance: {info.balance} {info.currency}"
        )
        return True

    def disconnect(self) -> None:
        """Shut down MT5 connection."""
        mt5.shutdown()
        self._connected = False
        logger.info("MT5 connection closed.")

    def is_connected(self) -> bool:
        return self._connected and mt5.terminal_info() is not None

    def switch_mode(self, mode: str) -> bool:
        """Switch between 'paper' and 'live' trading modes."""
        mode = mode.lower()
        if mode not in ("paper", "live"):
            logger.error(f"Invalid trading mode: {mode}")
            return False
        self.disconnect()
        self._trading_mode = mode
        self._credentials = self._load_credentials()
        success = self.connect()
        if success:
            save_mode(mode)   # persist so restart resumes with this mode
        return success

    # ------------------------------------------------------------------
    # Account Info
    # ------------------------------------------------------------------

    def get_account_info(self) -> Optional[dict]:
        """Return account balance, equity, margin, and metadata."""
        if not self.is_connected():
            logger.warning("get_account_info called while not connected.")
            return None

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

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return tick size, pip value, spread, and trading constraints."""
        info = mt5.symbol_info(symbol)
        if info is None:
            logger.warning(f"Symbol not found: {symbol}")
            return None

        # Ensure symbol is visible in Market Watch
        if not info.visible:
            if not mt5.symbol_select(symbol, True):
                logger.warning(f"symbol_select failed for {symbol}: {mt5.last_error()}")

        tick = mt5.symbol_info_tick(symbol)
        spread_pips = round(info.spread * info.point, 5)

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
        }

    def get_current_price(self, symbol: str) -> Optional[dict]:
        """Return current bid/ask for a symbol."""
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            logger.warning(f"No tick data for {symbol}: {mt5.last_error()}")
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

    def get_open_positions(self, symbol: Optional[str] = None) -> list[dict]:
        """Return all open positions, optionally filtered by symbol."""
        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            return []

        return [
            {
                "ticket":      p.ticket,
                "symbol":      p.symbol,
                "type":        "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
                "volume":      p.volume,
                "open_price":  p.price_open,
                "sl":          p.sl,
                "tp":          p.tp,
                "profit":      p.profit,
                "swap":        p.swap,
                "open_time":   datetime.fromtimestamp(p.time, tz=timezone.utc),
                "comment":     p.comment,
                "magic":       p.magic,
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

        deals = mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []

        return [
            {
                "ticket":    d.ticket,
                "order":     d.order,
                "symbol":    d.symbol,
                "type":      "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
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

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()
