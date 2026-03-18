"""
indicators.py – Technical indicator calculations used as model features.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from src.logger import get_logger

logger = get_logger("indicators")


class Indicators:
    """Computes a set of technical indicators on a price DataFrame."""

    def __init__(
        self,
        rsi_period: int = 14,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        bb_period: int = 20,
        bb_std: float = 2.0,
        ema_fast: int = 9,
        ema_slow: int = 21,
        atr_period: int = 14,
        stoch_k: int = 14,
        stoch_d: int = 3,
        stoch_smooth: int = 3,
    ) -> None:
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.atr_period = atr_period
        self.stoch_k = stoch_k
        self.stoch_d = stoch_d
        self.stoch_smooth = stoch_smooth

    # ------------------------------------------------------------------
    # Individual indicators
    # ------------------------------------------------------------------

    def rsi(self, close: pd.Series) -> pd.Series:
        """Relative Strength Index."""
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        avg_loss = loss.ewm(com=self.rsi_period - 1, min_periods=self.rsi_period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return (100 - 100 / (1 + rs)).rename("rsi")

    def macd(self, close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
        """MACD line, signal line, and histogram."""
        ema_f = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_s = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line = (ema_f - ema_s).rename("macd")
        signal_line = macd_line.ewm(span=self.macd_signal, adjust=False).mean().rename(
            "macd_signal"
        )
        histogram = (macd_line - signal_line).rename("macd_hist")
        return macd_line, signal_line, histogram

    def bollinger_bands(
        self, close: pd.Series
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Upper band, middle SMA, and lower band."""
        middle = close.rolling(self.bb_period).mean().rename("bb_middle")
        std = close.rolling(self.bb_period).std()
        upper = (middle + self.bb_std * std).rename("bb_upper")
        lower = (middle - self.bb_std * std).rename("bb_lower")
        return upper, middle, lower

    def ema(self, close: pd.Series) -> tuple[pd.Series, pd.Series]:
        """Fast and slow EMAs."""
        fast = close.ewm(span=self.ema_fast, adjust=False).mean().rename("ema_fast")
        slow = close.ewm(span=self.ema_slow, adjust=False).mean().rename("ema_slow")
        return fast, slow

    def atr(self, high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
        """Average True Range."""
        tr = pd.concat(
            [
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs(),
            ],
            axis=1,
        ).max(axis=1)
        return tr.ewm(com=self.atr_period - 1, min_periods=self.atr_period).mean().rename("atr")

    def stochastic(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> tuple[pd.Series, pd.Series]:
        """Stochastic oscillator %K and %D."""
        lowest_low = low.rolling(self.stoch_k).min()
        highest_high = high.rolling(self.stoch_k).max()
        denom = (highest_high - lowest_low).replace(0, np.nan)
        k = (100 * (close - lowest_low) / denom).rolling(self.stoch_smooth).mean().rename(
            "stoch_k"
        )
        d = k.rolling(self.stoch_d).mean().rename("stoch_d")
        return k, d

    # ------------------------------------------------------------------
    # Composite feature builder
    # ------------------------------------------------------------------

    def compute_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute all indicators and append them to a copy of *df*.

        Expects columns: open, high, low, close, tick_volume (or volume).
        Returns a new DataFrame with indicator columns added.
        """
        required = {"open", "high", "low", "close"}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame is missing required columns: {missing}")

        out = df.copy()
        close = out["close"]
        high = out["high"]
        low = out["low"]

        out["rsi"] = self.rsi(close)

        macd_line, signal_line, histogram = self.macd(close)
        out["macd"] = macd_line
        out["macd_signal"] = signal_line
        out["macd_hist"] = histogram

        bb_upper, bb_middle, bb_lower = self.bollinger_bands(close)
        out["bb_upper"] = bb_upper
        out["bb_middle"] = bb_middle
        out["bb_lower"] = bb_lower

        ema_fast, ema_slow = self.ema(close)
        out["ema_fast"] = ema_fast
        out["ema_slow"] = ema_slow

        out["atr"] = self.atr(high, low, close)

        stoch_k, stoch_d = self.stochastic(high, low, close)
        out["stoch_k"] = stoch_k
        out["stoch_d"] = stoch_d

        # Volume column normalisation
        vol_col = "tick_volume" if "tick_volume" in out.columns else "real_volume"
        if vol_col in out.columns:
            out["volume"] = out[vol_col]

        logger.debug("Computed indicators for %d rows.", len(out))
        return out
