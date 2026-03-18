"""
data_fetcher.py – Retrieves OHLCV price data from the MT5 terminal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.logger import get_logger

logger = get_logger("data_fetcher")

# Mapping from user-friendly timeframe strings to MT5 constants
TIMEFRAME_MAP: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 16385,
    "H4": 16388,
    "D1": 16408,
    "W1": 32769,
    "MN1": 49153,
}


def _get_mt5_timeframe(timeframe_str: str) -> int:
    """Convert a timeframe string to its MT5 integer constant."""
    if not MT5_AVAILABLE:
        return TIMEFRAME_MAP.get(timeframe_str.upper(), 15)

    tf_map = {
        "M1": mt5.TIMEFRAME_M1,      # type: ignore[union-attr]
        "M5": mt5.TIMEFRAME_M5,      # type: ignore[union-attr]
        "M15": mt5.TIMEFRAME_M15,    # type: ignore[union-attr]
        "M30": mt5.TIMEFRAME_M30,    # type: ignore[union-attr]
        "H1": mt5.TIMEFRAME_H1,      # type: ignore[union-attr]
        "H4": mt5.TIMEFRAME_H4,      # type: ignore[union-attr]
        "D1": mt5.TIMEFRAME_D1,      # type: ignore[union-attr]
        "W1": mt5.TIMEFRAME_W1,      # type: ignore[union-attr]
        "MN1": mt5.TIMEFRAME_MN1,    # type: ignore[union-attr]
    }
    key = timeframe_str.upper()
    if key not in tf_map:
        raise ValueError(f"Unsupported timeframe: {timeframe_str!r}. Choose from {list(tf_map)}")
    return tf_map[key]


class DataFetcher:
    """Fetches historical and live OHLCV bars from the MT5 terminal."""

    def __init__(self, symbol: str, timeframe: str) -> None:
        self.symbol = symbol
        self.timeframe_str = timeframe.upper()
        self.timeframe = _get_mt5_timeframe(timeframe)

    def fetch_rates(self, count: int) -> Optional[pd.DataFrame]:
        """Fetch the last *count* closed OHLCV bars for the configured symbol/timeframe.

        Returns:
            A DataFrame with columns [time, open, high, low, close, tick_volume,
            spread, real_volume] or None on failure.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.error("MetaTrader5 package unavailable; cannot fetch rates.")
            return None

        rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, count)  # type: ignore[union-attr]
        if rates is None or len(rates) == 0:
            logger.error(
                "No rates returned for %s %s: %s",
                self.symbol,
                self.timeframe_str,
                mt5.last_error(),  # type: ignore[union-attr]
            )
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        logger.debug("Fetched %d bars for %s %s", len(df), self.symbol, self.timeframe_str)
        return df

    def fetch_rates_range(
        self, date_from: datetime, date_to: datetime
    ) -> Optional[pd.DataFrame]:
        """Fetch OHLCV bars between two datetimes.

        Returns:
            A DataFrame or None on failure.
        """
        if not MT5_AVAILABLE or mt5 is None:
            logger.error("MetaTrader5 package unavailable; cannot fetch rates.")
            return None

        rates = mt5.copy_rates_range(self.symbol, self.timeframe, date_from, date_to)  # type: ignore[union-attr]
        if rates is None or len(rates) == 0:
            logger.error(
                "No rates returned for %s %s range %s – %s: %s",
                self.symbol,
                self.timeframe_str,
                date_from,
                date_to,
                mt5.last_error(),  # type: ignore[union-attr]
            )
            return None

        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df.set_index("time", inplace=True)
        logger.debug(
            "Fetched %d bars for %s %s (%s – %s)",
            len(df),
            self.symbol,
            self.timeframe_str,
            date_from,
            date_to,
        )
        return df

    def get_symbol_info(self) -> Optional[object]:
        """Return MT5 SymbolInfo for the configured symbol, or None on failure."""
        if not MT5_AVAILABLE or mt5 is None:
            return None
        info = mt5.symbol_info(self.symbol)  # type: ignore[union-attr]
        if info is None:
            logger.warning("Symbol %s not found in MT5 terminal.", self.symbol)
        return info

    def get_current_price(self) -> Optional[dict]:
        """Return the latest bid/ask prices as a dict, or None on failure."""
        if not MT5_AVAILABLE or mt5 is None:
            return None
        tick = mt5.symbol_info_tick(self.symbol)  # type: ignore[union-attr]
        if tick is None:
            logger.warning("Could not retrieve tick for %s.", self.symbol)
            return None
        return {"bid": tick.bid, "ask": tick.ask, "time": tick.time}
