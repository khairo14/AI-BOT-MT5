"""
Market Regime Classifier

Classifies the current market condition for a symbol into one of six labels:
    trending_bull    — ADX strong, EMA50 > EMA200, upward momentum
    trending_bear    — ADX strong, EMA50 < EMA200, downward momentum
    ranging_low_vol  — ADX weak, ATR% low, sideways chop
    ranging_high_vol — ADX weak, ATR% elevated, noisy/volatile range
    volatile_breakout— ADX rising rapidly, ATR% spike, fresh momentum burst
    quiet            — Very low ATR%, barely moving (news-watch or off-hours)

Usage:
    from engine.regime_classifier import regime_classifier
    label = regime_classifier.classify("BTCUSD", df)   # → "volatile_breakout"

The classifier applies hysteresis: a new label must be stable for N bars
before it replaces the current label. This prevents whipsaw during transitions.

Per-asset-class ADX thresholds are used because crypto/indices have structurally
higher ATR and ADX readings than forex majors.
"""

from __future__ import annotations

import threading
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

# ── ADX trend threshold by asset class ──────────────────────────────────────
# Minimum ADX value considered a "trending" market.
# Crypto/indices trend at higher absolute ADX than forex.
_ADX_TREND_MIN: dict[str, float] = {
    "crypto":     30.0,   # BTC, ETH, XRP, SOL, etc.
    "indices":    28.0,   # US30, US100, GER40, etc.
    "commodities":25.0,   # XAUUSD, XAGUSD, USOIL, etc.
    "forex":      22.0,   # default for FX pairs
}

# ADX breakout threshold: ADX rising above this fast signals a breakout regime
_ADX_BREAKOUT_MIN: dict[str, float] = {
    "crypto":     40.0,
    "indices":    35.0,
    "commodities":32.0,
    "forex":      30.0,
}

# ATR% thresholds (ATR14 / close * 100)
_ATR_QUIET_MAX:  float = 0.10   # below this → quiet (almost no movement)
_ATR_HIGH_VOL:   float = 0.80   # above this → high-vol ranging (if ADX weak)
_ATR_BREAKOUT:   float = 1.20   # above this + ADX rising → volatile breakout

# Hysteresis: a new label must hold for at least N consecutive bars
_HYSTERESIS_BARS = 3


def _asset_class(symbol: str) -> str:
    """Map a symbol string to its asset class for threshold selection."""
    s = symbol.upper()
    if any(x in s for x in ("BTC", "ETH", "XRP", "SOL", "ADA", "DOT", "MATIC", "LTC", "BNB")):
        return "crypto"
    if any(x in s for x in ("US30", "US100", "US500", "SPX", "NDX",
                              "GER40", "UK100", "FRA40", "JPN225", "AUS200", "ESP35")):
        return "indices"
    if any(x in s for x in ("XAU", "GOLD", "XAG", "SILVER", "OIL", "BRENT", "WTI",
                              "USOIL", "UKOIL", "NGAS", "COPPER")):
        return "commodities"
    return "forex"


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def _adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute ADX(period). Returns array aligned with input (NaN for first period bars)."""
    n = len(close)
    if n < period + 1:
        return np.full(n, np.nan)

    # True Range
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))

    # Directional movement
    up_move   = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Smooth with Wilder's method
    def wilder_smooth(arr: np.ndarray, p: int) -> np.ndarray:
        out = np.empty(len(arr))
        out[0] = arr[:p].mean()
        for i in range(1, len(arr)):
            out[i] = out[i - 1] - (out[i - 1] / p) + arr[i]
        return out

    tr_smooth  = wilder_smooth(tr[1:],      period)
    pdm_smooth = wilder_smooth(plus_dm,     period)
    mdm_smooth = wilder_smooth(minus_dm,    period)

    eps = 1e-10
    pdi = 100.0 * pdm_smooth / (tr_smooth + eps)
    mdi = 100.0 * mdm_smooth / (tr_smooth + eps)
    dx  = 100.0 * np.abs(pdi - mdi) / (pdi + mdi + eps)

    adx_arr = wilder_smooth(dx, period)

    # Pad front with NaN to match input length
    pad = np.full(n - len(adx_arr), np.nan)
    return np.concatenate([pad, adx_arr])


def _classify_raw(
    df: pd.DataFrame,
    symbol: str,
) -> str:
    """Compute the raw (un-hysteresis'd) regime label from OHLCV bars."""
    if len(df) < 210:
        return "quiet"

    close = np.asarray(df["close"], dtype=float)
    high  = np.asarray(df["high"],  dtype=float)
    low   = np.asarray(df["low"],   dtype=float)

    asset_cls  = _asset_class(symbol)
    adx_trend  = _ADX_TREND_MIN[asset_cls]
    adx_brk    = _ADX_BREAKOUT_MIN[asset_cls]

    # ADX
    adx_arr = _adx(high, low, close, period=14)
    if np.isnan(adx_arr[-1]):
        return "quiet"
    curr_adx  = float(adx_arr[-1])
    prev_adx  = float(adx_arr[-4]) if not np.isnan(adx_arr[-4]) else curr_adx
    adx_rising = curr_adx > prev_adx + 2.0   # rising ≥2 units over 3 bars

    # ATR%
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().values
    eps = max(abs(close[-1]), 1e-8)
    atr_pct = float(atr14[-1] / eps * 100.0)

    # EMA50/200 direction
    ema50  = _ema(close, 50)
    ema200 = _ema(close, 200)
    bull = ema50[-1] > ema200[-1]

    # Classification logic
    if atr_pct < _ATR_QUIET_MAX:
        return "quiet"

    if curr_adx >= adx_brk and atr_pct >= _ATR_BREAKOUT and adx_rising:
        return "volatile_breakout"

    if curr_adx >= adx_trend:
        return "trending_bull" if bull else "trending_bear"

    # ADX weak → ranging
    if atr_pct >= _ATR_HIGH_VOL:
        return "ranging_high_vol"
    return "ranging_low_vol"


class RegimeClassifier:
    """
    Thread-safe market regime classifier with per-symbol hysteresis.

    The classifier maintains a pending label and a stability counter for each
    symbol. A new raw label must persist for _HYSTERESIS_BARS consecutive
    classifications before it replaces the confirmed label. This prevents the
    weight vector from oscillating at regime boundaries.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # symbol → confirmed label
        self._labels:  dict[str, str]  = {}
        # symbol → (pending_label, bars_held)
        self._pending: dict[str, tuple[str, int]] = {}

    def classify(self, symbol: str, df: pd.DataFrame) -> str:
        """
        Return the current hysteresis-stable regime label for this symbol.

        Args:
            symbol: MT5 symbol name (e.g. "EURUSD", "BTCUSD")
            df:     Primary-timeframe OHLCV DataFrame, most-recent bar last.
                    Requires columns: open, high, low, close, volume.
                    Minimum 210 rows for reliable computation.

        Returns:
            One of: "trending_bull", "trending_bear", "ranging_low_vol",
                    "ranging_high_vol", "volatile_breakout", "quiet"
        """
        try:
            raw = _classify_raw(df, symbol)
        except Exception as exc:
            logger.debug(f"RegimeClassifier error [{symbol}]: {exc}")
            return self._labels.get(symbol, "quiet")

        with self._lock:
            confirmed = self._labels.get(symbol)
            pending_label, pending_count = self._pending.get(symbol, (raw, 0))

            if raw == pending_label:
                pending_count += 1
            else:
                # New raw label observed — start fresh pending countdown
                pending_label  = raw
                pending_count  = 1

            if pending_count >= _HYSTERESIS_BARS or confirmed is None:
                # Promote pending to confirmed
                self._labels[symbol] = raw
                confirmed = raw

            self._pending[symbol] = (pending_label, pending_count)

            if confirmed != raw and pending_count < _HYSTERESIS_BARS:
                logger.debug(
                    f"Regime [{symbol}]: confirmed={confirmed}, "
                    f"pending={raw} ({pending_count}/{_HYSTERESIS_BARS})"
                )

            return confirmed

    def current_label(self, symbol: str) -> Optional[str]:
        """Return the last confirmed label without updating, or None if never classified."""
        with self._lock:
            return self._labels.get(symbol)

    def all_labels(self) -> dict[str, str]:
        """Return a snapshot of all confirmed labels (for dashboard/API exposure)."""
        with self._lock:
            return dict(self._labels)


# Application-level singleton
regime_classifier = RegimeClassifier()
