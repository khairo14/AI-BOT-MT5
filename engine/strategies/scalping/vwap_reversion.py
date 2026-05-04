"""
S3 — VWAP Reversion (SCALPING — M5 PRIMARY)

Strategy:
  When price penetrates beyond sigma_entry standard deviations from VWAP
  AND RSI confirms oversold/overbought, enter a mean-reversion trade
  targeting VWAP (TP1) with runner to opposite band (TP2).

Edge: Price has a statistical tendency to revert to VWAP after extreme
      deviations, especially in ranging/low-volatility regimes. This
      strategy is gated to ONLY run in ranging_low_vol, ranging_high_vol,
      and quiet regimes by strategy_runner.py.

Fixes applied:
  - Single calculate() method (removed duplicate with Stochastic)
  - M5 primary timeframe (matches ema_scalp, bb_squeeze conventions)
  - VWAP + std pre-computed once per dataframe (not re-computed on slices)
  - All params exposed in optimizer grid
  - RSI confirmation always required (no penetration bypass)
  - Adaptive volatility filter using ATR% instead of hardcoded range check
  - Volume confirmation — avoid low-liquidity snap-throughs
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import ta

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult


DEFAULT_PARAMS = {
    # ── VWAP bands ──────────────────────────────────────────────────────────
    "sigma_entry": 1.2,           # entry when price is this many stds from VWAP
    "sigma_sl": 2.5,              # stop-loss at this many stds from VWAP
    "vwap_std_window": 20,        # rolling window for VWAP std deviation (M5 bars)

    # ── RSI confirmation ────────────────────────────────────────────────────
    "rsi_period": 14,
    "rsi_oversold": 32,           # RSI must be ≤ this for BUY
    "rsi_overbought": 68,         # RSI must be ≥ this for SELL

    # ── Filters ─────────────────────────────────────────────────────────────
    "min_rr_to_vwap": 0.6,        # minimum R:R to VWAP to take the trade
    "atr_vol_filter": 1.2,        # ATR% above which trades are blocked (adaptive vol filter)
    "atr_period": 14,             # period for ATR calculation
    "min_volume_ratio": 0.5,      # last bar volume / 20-bar avg — filter low-liquidity
    "max_spread_pips": 3.0,       # max allowed spread (enforced by strategy_runner)
}


def _ensure_vwap(df: pd.DataFrame, window: int) -> pd.DataFrame:
    if "vwap" in df.columns and "vwap_std" in df.columns:
        if "typical" not in df.columns:
            df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
        return df

    # Compute typical price and add as column — needed by both paths
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
    
    if "date" not in df.columns:
        if "time" in df.columns:
            df["date"] = df["time"].dt.date

    if "date" in df.columns:
        df["cum_tp_vol"] = (df["typical"] * df["volume"]).groupby(df["date"]).cumsum()
        df["cum_vol"] = df["volume"].groupby(df["date"]).cumsum()
        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)
        df["vwap_std"] = df.groupby("date")["typical"].transform(
            lambda x: x.rolling(window, min_periods=10).std()
        )
    else:
        df["cum_tp_vol"] = (df["typical"] * df["volume"]).cumsum()
        df["cum_vol"] = df["volume"].cumsum()
        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)
        df["vwap_std"] = df["typical"].rolling(window, min_periods=10).std()

    return df


def _compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Return ATR as percentage of current price."""
    if len(df) < period + 1:
        return 0.0
    high = df["high"].values.astype(float)
    low = df["low"].values.astype(float)
    close = df["close"].values.astype(float)
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(
        high - low,
        np.maximum(
            np.abs(high - prev_close),
            np.abs(low - prev_close),
        ),
    )
    atr = float(np.mean(tr[-period:]))
    current_price = abs(close[-1])
    if current_price == 0:
        return 0.0
    return (atr / current_price) * 100.0


class VWAPReversion(BaseStrategy):

    name = "vwap_reversion"
    trading_type = "scalping"
    timeframe = "M5"  # ← changed from M1 to M5 for system consistency

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        # ── Minimum data check ──────────────────────────────────────────────
        min_bars = max(p["vwap_std_window"], p["rsi_period"], p["atr_period"]) + 10
        if len(df) < min_bars:
            return self._no_signal()

        # ── Pre-compute VWAP (cached) ───────────────────────────────────────
        _ensure_vwap(df, p["vwap_std_window"])

        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"] if "volume" in df.columns else None

        # ── Current bar values ──────────────────────────────────────────────
        curr = df.iloc[-1]

        # Sanity: VWAP must be valid
        if np.isnan(curr.get("vwap", np.nan)) or np.isnan(curr.get("vwap_std", np.nan)):
            return self._no_signal()

        vwap = float(curr["vwap"])
        std = float(curr["vwap_std"])
        price = float(curr["close"])

        if std <= 0 or vwap <= 0:
            return self._no_signal()

        # ── Band calculations ───────────────────────────────────────────────
        upper_entry = vwap + p["sigma_entry"] * std
        lower_entry = vwap - p["sigma_entry"] * std
        upper_sl = vwap + p["sigma_sl"] * std
        lower_sl = vwap - p["sigma_sl"] * std

        # ── RSI ────────────────────────────────────────────────────────────
        rsi_series = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()
        curr_rsi = float(rsi_series.iloc[-1])
        if np.isnan(curr_rsi):
            return self._no_signal()

        # ── Volatility filter (adaptive ATR%) ───────────────────────────────
        atr_pct = _compute_atr(df, period=p["atr_period"])
        if atr_pct > p["atr_vol_filter"]:
            return self._no_signal()

        # ── Volume filter (avoid low-liquidity snap-throughs) ───────────────
        if volume is not None and len(volume) >= 20:
            avg_vol = float(volume.iloc[-20:].mean())
            last_vol = float(volume.iloc[-1])
            if avg_vol > 0 and last_vol / avg_vol < p["min_volume_ratio"]:
                return self._no_signal()

        # ── Indicators for UI / logging ─────────────────────────────────────
        indicators = {
            "vwap": round(vwap, 5),
            "vwap_std": round(std, 5),
            "rsi": round(curr_rsi, 2),
            "atr_pct": round(atr_pct, 3),
        }

        # ====================================================================
        # BUY SIGNAL — price below lower band + RSI oversold
        # ====================================================================
        if price <= lower_entry and curr_rsi <= p["rsi_oversold"]:
            sl = lower_sl
            tp1 = vwap          # primary target: reversion to mean
            tp2 = upper_entry   # runner: reversion to opposite band

            risk = price - sl
            reward = tp1 - price

            if risk <= 0:
                return self._no_signal(indicators)

            rr = reward / risk

            if rr < p["min_rr_to_vwap"]:
                return self._no_signal(indicators)

            if self._sl_too_close("BUY", price, sl):
                return self._no_signal(indicators)

            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=price,
                    sl_price=sl,
                    tp_price=tp1,
                    tp2_price=tp2,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="scalp|vwap_rev|buy",
                ),
                indicators=indicators,
            )

        # ====================================================================
        # SELL SIGNAL — price above upper band + RSI overbought
        # ====================================================================
        if price >= upper_entry and curr_rsi >= p["rsi_overbought"]:
            sl = upper_sl
            tp1 = vwap          # primary target: reversion to mean
            tp2 = lower_entry   # runner: reversion to opposite band

            risk = sl - price
            reward = price - tp1

            if risk <= 0:
                return self._no_signal(indicators)

            rr = reward / risk

            if rr < p["min_rr_to_vwap"]:
                return self._no_signal(indicators)

            if self._sl_too_close("SELL", price, sl):
                return self._no_signal(indicators)

            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=price,
                    sl_price=sl,
                    tp_price=tp1,
                    tp2_price=tp2,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="scalp|vwap_rev|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)