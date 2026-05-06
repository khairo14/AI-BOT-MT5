"""
S2 — Bollinger Band Squeeze Breakout
Timeframe: M5
Symbols: any forex, indices, commodities

Fixes applied:
  - ADX trend-strength filter — blocks squeeze breakouts in weak/choppy conditions
    where the "breakout" has no momentum behind it (matches ema_scalp pattern)
  - Squeeze bar cap — blocks overly long squeezes (>30 bars) where the market
    is structurally dead and the breakout is likely noise
"""

from __future__ import annotations

import ta
import numpy as np
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "bb_period": 20,
    "bb_std": 2.0,
    "roc_period": 5,
    "min_squeeze_bars": 3,
    "max_squeeze_bars": 30,      # new: cap on squeeze duration (0 = disabled)
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 1.5,         # partial close at 1.5×ATR
    "tp_atr_mult": 2.5,          # tp2 (full close) at 2.5×ATR
    "max_spread_pips": 4.0,
    "vol_confirm_mult": 1.2,     # volume must exceed 20-bar avg × this (0 = disabled)
    # ADX trend-strength filter — blocks breakouts without momentum
    "adx_period": 14,
    "adx_min": 18,               # require ADX > this before allowing entry
    # EMA trend filter — breakout must align with the medium-term trend direction
    "ema_trend_period": 50,
    "require_trend_align": True,
    "atr_period": 14,
}


class BBSqueeze(BaseStrategy):

    name = "bb_squeeze"
    trading_type = "scalping"
    timeframe = "M5"

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < max(p["bb_period"] + p["min_squeeze_bars"] + 5, p["ema_trend_period"] + 5, p["adx_period"] + 5):
            return self._no_signal()

        close = df["close"]
        high = df["high"]
        low = df["low"]

        # --- Indicators ---
        bb = ta.volatility.BollingerBands(
            close, window=p["bb_period"], window_dev=p["bb_std"]
        )
        upper = bb.bollinger_hband()
        lower = bb.bollinger_lband()
        mid   = bb.bollinger_mavg()
        width = upper - lower

        atr = ta.volatility.AverageTrueRange(high, low, close, window=p["atr_period"]).average_true_range()

        # ROC as momentum direction
        roc = close.pct_change(periods=p["roc_period"]) * 100

        # ADX — trend strength filter (new)
        adx_ind = ta.trend.ADXIndicator(high, low, close, window=p["adx_period"])
        curr_adx = adx_ind.adx().iloc[-1]

        # Squeeze: BB width < rolling average of BB width
        avg_width = width.rolling(window=p["bb_period"]).mean()

        # EMA trend filter
        ema_trend = ta.trend.EMAIndicator(close, window=p["ema_trend_period"]).ema_indicator()
        curr_ema_trend = ema_trend.iloc[-1]

        curr_close = close.iloc[-1]
        curr_upper = upper.iloc[-1]
        curr_lower = lower.iloc[-1]
        curr_width = width.iloc[-1]
        curr_avg_width = avg_width.iloc[-1]
        curr_roc = roc.iloc[-1]
        curr_atr = atr.iloc[-1]

        # Count consecutive squeeze bars ending on previous candle
        squeeze_count = 0
        for i in range(2, len(df)):
            if width.iloc[-i] < avg_width.iloc[-i]:
                squeeze_count += 1
            else:
                break

        # ADX gate — block entries when market lacks momentum
        adx_ok = (not pd.isna(curr_adx)) and (curr_adx >= p["adx_min"])

        # Squeeze cap — block overly long squeezes (market structurally dead)
        max_sqz = p.get("max_squeeze_bars", 0)
        squeeze_ok = squeeze_count <= max_sqz if max_sqz > 0 else True

        indicators = {
            "bb_upper": round(curr_upper, 5),
            "bb_lower": round(curr_lower, 5),
            "bb_width": round(curr_width, 6),
            "avg_width": round(curr_avg_width, 6),
            "roc": round(curr_roc, 4),
            "squeeze_bars": squeeze_count,
            "atr": round(curr_atr, 5),
            "adx": round(curr_adx, 2) if not pd.isna(curr_adx) else None,
            "adx_ok": adx_ok,
            "squeeze_ok": squeeze_ok,
            "ema_trend": round(curr_ema_trend, 5),
            "price_above_ema": bool(curr_close > curr_ema_trend),
        }

        # Need at least min_squeeze_bars of prior squeeze before breakout
        if squeeze_count < p["min_squeeze_bars"]:
            return self._no_signal(indicators)

        # Squeeze cap check
        if not squeeze_ok:
            return self._no_signal(indicators)

        # ADX gate
        if not adx_ok:
            return self._no_signal(indicators)

        # Volume confirmation
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        # Trend alignment
        require_align = p.get("require_trend_align", True)
        trend_bullish = curr_close > curr_ema_trend
        trend_bearish = curr_close < curr_ema_trend

        # Breakout up
        if (
            curr_close > curr_upper
            and curr_roc > 0
            and vol_ok
            and (not require_align or trend_bullish)
        ):
            sl  = round(curr_close - p["sl_atr_mult"] * curr_atr, 5)
            tp1 = round(curr_close + p["tp1_atr_mult"] * curr_atr, 5)
            tp2 = round(curr_close + p["tp_atr_mult"] * curr_atr, 5)
            if self._sl_too_close("BUY", curr_close, sl):
                return self._no_signal(indicators)
            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp1,
                    tp2_price=tp2,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="scalp|bb_squeeze|buy",
                ),
                indicators=indicators,
            )

        # Breakout down
        if (
            curr_close < curr_lower
            and curr_roc < 0
            and vol_ok
            and (not require_align or trend_bearish)
        ):
            sl  = round(curr_close + p["sl_atr_mult"] * curr_atr, 5)
            tp1 = round(curr_close - p["tp1_atr_mult"] * curr_atr, 5)
            tp2 = round(curr_close - p["tp_atr_mult"] * curr_atr, 5)
            if self._sl_too_close("SELL", curr_close, sl):
                return self._no_signal(indicators)
            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp1,
                    tp2_price=tp2,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="scalp|bb_squeeze|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)