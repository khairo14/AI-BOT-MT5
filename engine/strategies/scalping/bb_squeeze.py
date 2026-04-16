"""
S2 — Bollinger Band Squeeze Breakout
Timeframe: M2/M5
Symbols: GBPUSD, US30Cash, US100Cash
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
    "min_squeeze_bars": 5,
    "sl_atr_mult": 1.5,
    "tp1_atr_mult": 1.5,    # partial close at 1.5×ATR
    "tp_atr_mult": 2.5,     # tp2 (full close) at 2.5×ATR
    "max_spread_pips": 2.0,
    "vol_confirm_mult": 1.2, # volume must exceed 20-bar avg × this (0 = disabled)
}


class BBSqueeze(BaseStrategy):

    name = "bb_squeeze"
    trading_type = "scalping"
    timeframe = "M5"

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < p["bb_period"] + p["min_squeeze_bars"] + 5:
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

        atr = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()

        # ROC as momentum direction
        roc = close.pct_change(periods=p["roc_period"]) * 100

        # Squeeze: BB width < rolling average of BB width
        avg_width = width.rolling(window=p["bb_period"]).mean()

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

        indicators = {
            "bb_upper": round(curr_upper, 5),
            "bb_lower": round(curr_lower, 5),
            "bb_width": round(curr_width, 6),
            "avg_width": round(curr_avg_width, 6),
            "roc": round(curr_roc, 4),
            "squeeze_bars": squeeze_count,
            "atr": round(curr_atr, 5),
        }

        # Need at least min_squeeze_bars of prior squeeze before breakout
        if squeeze_count < p["min_squeeze_bars"]:
            return self._no_signal(indicators)

        # Volume confirmation: breakout bar must show expanded volume
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        # Breakout up: current close breaks above upper band
        if curr_close > curr_upper and curr_roc > 0 and vol_ok:
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

        # Breakout down: current close breaks below lower band
        if curr_close < curr_lower and curr_roc < 0 and vol_ok:
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
