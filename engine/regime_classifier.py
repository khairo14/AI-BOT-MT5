"""
Market Regime Classifier

Classifies each symbol's market regime using ADX, ATR, and price structure.
Used by strategies to gate entries based on regime compatibility.

Regime labels:
  - trending_bull       (ADX >= 25, EMA50 > EMA200)
  - trending_bear       (ADX >= 25, EMA50 < EMA200)
  - ranging_low_vol     (ADX < 20, ATR% low)
  - ranging_high_vol    (ADX < 20, ATR% high)
  - volatile_breakout   (ADX >= 25, ATR% high, expanding ranges)
  - quiet               (ADX < 15, ATR% very low)

No external config files required — all thresholds are auto-adaptive
based on historical ATR percentiles per symbol.
"""

from __future__ import annotations

import threading
import time
from functools import lru_cache
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

# ── Default thresholds (used when insufficient history) ─────────────────────
_DEFAULT_ADX_TREND = 25
_DEFAULT_ADX_RANGE = 20
_DEFAULT_ADX_QUIET = 15

# Volatility percentiles (based on each symbol's own history)
_ATR_PERCENTILE_HIGH = 70   # top 30% = high volatility
_ATR_PERCENTILE_LOW  = 30   # bottom 30% = low volatility

# Cache TTL (seconds) - regime classification is expensive, cache results
_CACHE_TTL = 60  # 1 minute

# Dynamic category detection from symbol name
def _detect_category(symbol: str) -> str:
    """Auto-detect asset category from symbol name."""
    clean = symbol.rstrip("#+*!").upper()
    
    # Crypto
    crypto = {"BTC", "ETH", "XRP", "SOL", "ADA", "DOGE", "DOT", "LTC", "BNB", "XLM", "ETC", "GRT"}
    if any(c in clean for c in crypto):
        return "crypto"
    
    # Indices
    indices = {"US30", "US100", "US500", "SPX", "NAS", "GER40", "DAX", "UK100", "FRA40", "JPN225", "AUS200"}
    if any(idx in clean for idx in indices):
        return "index"
    
    # Commodities
    commodities = {"GOLD", "XAU", "SILVER", "XAG", "OIL", "BRENT", "WTI", "NGAS", "GAS"}
    if any(cmd in clean for cmd in commodities):
        return "commodity"
    
    # Stocks (company names)
    stocks = {"APPLE", "TESLA", "NVDA", "MICROSOFT", "AMAZON", "GOOGLE", "META", "NETFLIX", "ADV"}
    if any(stk in clean for stk in stocks):
        return "stock"
    
    # Forex (6 letters, all alphabetical)
    if len(clean) == 6 and clean.isalpha():
        return "forex"
    
    return "forex"  # default


def _get_category_thresholds(category: str) -> tuple[float, float, float]:
    """
    Return (adx_trend, adx_range, atr_baseline_pct) for the category.
    Thresholds are per-category because crypto needs higher ADX to confirm trend,
    while forex trends can be detected at lower ADX.
    """
    thresholds = {
        "forex":     (25, 20, 0.15),   # ADX trend, range; baseline ATR%
        "crypto":    (35, 25, 1.00),   # Crypto needs stronger confirmation
        "index":     (25, 20, 0.30),   # Indices similar to forex, higher vol
        "commodity": (28, 22, 0.40),   # Gold/Oil in between
        "stock":     (30, 22, 0.50),   # Individual stocks
    }
    return thresholds.get(category, (25, 20, 0.15))


class RegimeClassifier:
    """
    Thread-safe market regime classifier with per-symbol caching.
    No external config files — all thresholds are dynamic based on
    each symbol's own historical volatility distribution.
    """

    def __init__(self):
        self._cache: dict[str, tuple[str, float]] = {}  # symbol → (regime, timestamp)
        self._lock = threading.RLock()
        self._atr_baselines: dict[str, float] = {}     # symbol → baseline ATR% (50th percentile)
        self._atr_high_thresholds: dict[str, float] = {}  # symbol → high vol threshold (70th percentile)
        self._atr_low_thresholds: dict[str, float] = {}   # symbol → low vol threshold (30th percentile)

    # ── Public API ────────────────────────────────────────────────────────────

    def classify(
        self,
        symbol: str,
        df: pd.DataFrame,
        timeframe: str = "H1",
        force_refresh: bool = False,
    ) -> str:
        """
        Classify the current market regime for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., "EURUSD")
            df: OHLCV DataFrame with columns: open, high, low, close
            timeframe: Timeframe string (used for cache key)
            force_refresh: If True, ignore cache and recompute
        
        Returns:
            Regime label: trending_bull | trending_bear | ranging_low_vol |
                          ranging_high_vol | volatile_breakout | quiet
        """
        if df is None or len(df) < 50:
            return "quiet"

        cache_key = f"{symbol}_{timeframe}"
        
        with self._lock:
            if not force_refresh and cache_key in self._cache:
                regime, timestamp = self._cache[cache_key]
                if time.time() - timestamp < _CACHE_TTL:
                    return regime

        # Compute regime
        regime = self._classify_raw(df, symbol)
        
        with self._lock:
            self._cache[cache_key] = (regime, time.time())
        
        return regime

    # ── Internal classification ──────────────────────────────────────────────

    def _classify_raw(self, df: pd.DataFrame, symbol: str) -> str:
        """Core classification logic using raw OHLCV data."""
        if len(df) < 50:
            return "quiet"

        close = np.asarray(df["close"].to_numpy(dtype=float), dtype=float)
        high = np.asarray(df["high"].to_numpy(dtype=float), dtype=float)
        low = np.asarray(df["low"].to_numpy(dtype=float), dtype=float)

        # ── 1. Trend strength via ADX ────────────────────────────────────────
        adx = self._compute_adx(high, low, close, period=14)
        current_adx = adx[-1] if len(adx) > 0 else 0

        # ── 2. Trend direction via EMA50/EMA200 ──────────────────────────────
        ema50 = self._ema(close, 50)
        ema200 = self._ema(close, 200)
        is_bull = ema50[-1] > ema200[-1]

        # ── 3. Volatility via ATR% ───────────────────────────────────────────
        atr_pct = self._compute_atr_pct(high, low, close, period=14)
        current_atr_pct = atr_pct[-1] if len(atr_pct) > 0 else 0.0

        # Get dynamic volatility thresholds for this symbol
        low_thresh, high_thresh = self._get_volatility_thresholds(symbol, atr_pct)
        
        # Get category thresholds
        category = _detect_category(symbol)
        adx_trend, adx_range, _ = _get_category_thresholds(category)

        # ── 4. Price structure for breakout detection ────────────────────────
        bb_upper, bb_lower = self._bollinger_bands(close, period=20, std=2)

        bb_width_series = np.where(
            close > 0,
            (bb_upper - bb_lower) / close,
            0.0,
        )

        current_bb_width = float(bb_width_series[-1]) if len(bb_width_series) else 0.0
        bb_width_expanding = (
            current_bb_width > float(np.percentile(bb_width_series[-50:], 80))
            if len(bb_width_series) >= 50
            else False
        )
        # ── 5. Decision tree ─────────────────────────────────────────────────
        
        # Quiet market (no movement)
        if current_adx < _DEFAULT_ADX_QUIET and current_atr_pct < low_thresh:
            return "quiet"
        
        # Ranging markets
        if current_adx < adx_range:
            if current_atr_pct >= high_thresh:
                return "ranging_high_vol"
            return "ranging_low_vol"
        
        # Trending markets
        if current_adx >= adx_trend:
            # Volatile breakout (trending + expanding range)
            if bb_width_expanding and current_atr_pct >= high_thresh:
                return "volatile_breakout"
            return "trending_bull" if is_bull else "trending_bear"
        
        # Fallback for borderline ADX (20-25)
        if current_atr_pct >= high_thresh:
            return "volatile_breakout"
        return "trending_bull" if is_bull else "trending_bear"

    def _get_volatility_thresholds(self, symbol: str, atr_pct_series: np.ndarray) -> tuple[float, float]:
        """
        Return (low_threshold, high_threshold) for volatility based on
        this symbol's own historical ATR% distribution.
        
        This makes thresholds dynamic per symbol — crypto naturally has
        higher volatility and won't be misclassified as "volatile_breakout"
        during normal conditions.
        """
        # Use cached thresholds if available
        with self._lock:
            if symbol in self._atr_high_thresholds and symbol in self._atr_low_thresholds:
                return self._atr_low_thresholds[symbol], self._atr_high_thresholds[symbol]
        
        # Need at least 50 bars to compute percentiles
        if len(atr_pct_series) >= 50:
            low_thresh = np.percentile(atr_pct_series[-50:], _ATR_PERCENTILE_LOW)
            high_thresh = np.percentile(atr_pct_series[-50:], _ATR_PERCENTILE_HIGH)
            baseline = np.percentile(atr_pct_series[-50:], 50)
        else:
            # Fallback to category defaults
            category = _detect_category(symbol)
            _, _, baseline_pct = _get_category_thresholds(category)
            low_thresh = baseline_pct * 0.6
            high_thresh = baseline_pct * 1.5
        
        with self._lock:
            self._atr_low_thresholds[symbol] = float(low_thresh)
            self._atr_high_thresholds[symbol] = float(high_thresh)
            self._atr_baselines[symbol] = float(baseline if 'baseline' in locals() else baseline_pct)
        
        return float(low_thresh), float(high_thresh)

    # ── Technical indicators ─────────────────────────────────────────────────

    def _compute_adx(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """Calculate ADX (Average Directional Index)."""
        if len(high) < period + 1:
            return np.array([0.0])
        
        prev_close = np.concatenate([[close[0]], close[:-1]])
        
        # True Range
        tr = np.maximum(high - low, np.maximum(
            np.abs(high - prev_close),
            np.abs(low - prev_close)
        ))
        
        # Directional Movement
        up = high - prev_close
        down = -(low - prev_close)
        
        plus_dm = np.where((up > down) & (up > 0), up, 0.0)
        minus_dm = np.where((down > up) & (down > 0), down, 0.0)
        
        # Smooth with EMA
        atr = self._ema(tr, period)
        plus_di = 100 * self._ema(plus_dm, period) / atr
        minus_di = 100 * self._ema(minus_dm, period) / atr
        
        dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
        adx = self._ema(dx, period)
        
        return adx

    def _compute_atr_pct(self, high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
        """Calculate ATR as percentage of close price."""
        if len(high) < period + 1:
            return np.array([0.0])
        
        prev_close = np.concatenate([[close[0]], close[:-1]])
        
        tr = np.maximum(high - low, np.maximum(
            np.abs(high - prev_close),
            np.abs(low - prev_close)
        ))
        
        atr = self._ema(tr, period)
        atr_pct = 100 * atr / (close + 1e-10)
        
        return atr_pct

    def _ema(self, values: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average."""
        if len(values) == 0:
            return values
        
        alpha = 2.0 / (period + 1)
        ema = np.zeros_like(values)
        ema[0] = values[0]
        
        for i in range(1, len(values)):
            ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
        
        return ema

    def _bollinger_bands(
        self, close: np.ndarray, period: int = 20, std: float = 2.0
    ) -> tuple[np.ndarray, np.ndarray]:
        """Calculate Bollinger Bands (upper, lower)."""
        if len(close) < period:
            return np.array([close[-1]]), np.array([close[-1]])
        
        sma = np.convolve(close, np.ones(period) / period, mode='valid')
        sma_full = np.pad(sma, (period - 1, 0), constant_values=sma[0])
        
        # Rolling standard deviation
        std_dev = np.zeros_like(close)
        for i in range(period - 1, len(close)):
            std_dev[i] = np.std(close[i - period + 1:i + 1])
        
        upper = sma_full + std * std_dev
        lower = sma_full - std * std_dev
        
        return upper, lower


# Application singleton
regime_classifier = RegimeClassifier()