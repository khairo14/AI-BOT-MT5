"""
D3 — RSI Divergence
Timeframe: H1 (divergence), M30 (entry)
Symbols: GBPUSD, EURJPY, GOLD, GBPJPY, GER40Cash
"""

from __future__ import annotations

import ta
import pandas as pd
import numpy as np

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "rsi_period": 14,
    "ema_bias_period": 50,
    "divergence_lookback": 20,
    "atr_period": 14,
    "tp_rr": 1.5,   # tp1 (partial close), also fallback if no tp2_rr
    "tp2_rr": 2.5,  # tp2 (full close)
}


class RSIDivergence(BaseStrategy):

    name = "rsi_divergence"
    trading_type = "day_trading"
    timeframe = "M30"

    def calculate(self, df: pd.DataFrame, df_h1: pd.DataFrame | None = None) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < 30:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        rsi = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()
        ema_bias = ta.trend.EMAIndicator(close, window=p["ema_bias_period"]).ema_indicator()
        atr = ta.volatility.AverageTrueRange(high, low, close, window=p["atr_period"]).average_true_range()

        lb = p["divergence_lookback"]
        price_slice = close.iloc[-lb:]
        rsi_slice   = rsi.iloc[-lb:]

        # -- Bullish divergence: price lower low, RSI higher low --
        price_low_idx  = price_slice.idxmin()
        price_low_val  = price_slice.min()
        price_prev_low = price_slice[:price_slice.index.get_loc(price_low_idx)].min() \
            if price_slice.index.get_loc(price_low_idx) > 0 else None

        rsi_at_low     = rsi_slice.loc[price_low_idx] if price_low_idx in rsi_slice.index else None
        rsi_prev_low   = rsi_slice[:rsi_slice.index.get_loc(price_low_idx)].min() \
            if rsi_at_low is not None and rsi_slice.index.get_loc(price_low_idx) > 0 else None

        # -- Bearish divergence: price higher high, RSI lower high --
        price_high_idx = price_slice.idxmax()
        price_high_val = price_slice.max()
        price_prev_high = price_slice[:price_slice.index.get_loc(price_high_idx)].max() \
            if price_slice.index.get_loc(price_high_idx) > 0 else None

        rsi_at_high    = rsi_slice.loc[price_high_idx] if price_high_idx in rsi_slice.index else None
        rsi_prev_high  = rsi_slice[:rsi_slice.index.get_loc(price_high_idx)].max() \
            if rsi_at_high is not None and rsi_slice.index.get_loc(price_high_idx) > 0 else None

        curr_close = close.iloc[-1]
        curr_rsi   = rsi.iloc[-1]
        curr_atr   = atr.iloc[-1]
        curr_ema   = ema_bias.iloc[-1]
        prev_rsi   = rsi.iloc[-2]

        bullish_div = (
            price_prev_low is not None
            and rsi_at_low is not None
            and rsi_prev_low is not None
            and price_low_val < price_prev_low        # price: lower low
            and rsi_at_low > rsi_prev_low             # RSI: higher low
            and curr_close > curr_ema                 # trade toward mean (above EMA)
        )

        bearish_div = (
            price_prev_high is not None
            and rsi_at_high is not None
            and rsi_prev_high is not None
            and price_high_val > price_prev_high      # price: higher high
            and rsi_at_high < rsi_prev_high           # RSI: lower high
            and curr_close < curr_ema                 # trade toward mean (below EMA)
        )

        # Confirmation: RSI crossing 50 in trade direction
        rsi_cross_up   = prev_rsi < 50 <= curr_rsi
        rsi_cross_down = prev_rsi > 50 >= curr_rsi

        indicators = {
            "rsi": round(curr_rsi, 2),
            "rsi_cross_up": rsi_cross_up,
            "rsi_cross_down": rsi_cross_down,
            "bullish_div": bullish_div,
            "bearish_div": bearish_div,
            "ema_bias": round(curr_ema, 5),
        }

        if bullish_div and rsi_cross_up:
            sl = round(price_low_val - curr_atr * 0.3, 5)
            sl_dist = abs(curr_close - sl)
            if self._sl_too_close("BUY", curr_close, sl):
                return self._no_signal(indicators)
            tp1 = round(curr_close + sl_dist * p["tp_rr"], 5)
            tp2 = round(curr_close + sl_dist * p["tp2_rr"], 5)
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
                    comment="day|rsi_div|buy",
                ),
                indicators=indicators,
            )

        if bearish_div and rsi_cross_down:
            sl = round(price_high_val + curr_atr * 0.3, 5)
            sl_dist = abs(sl - curr_close)
            if self._sl_too_close("SELL", curr_close, sl):
                return self._no_signal(indicators)
            tp1 = round(curr_close - sl_dist * p["tp_rr"], 5)
            tp2 = round(curr_close - sl_dist * p["tp2_rr"], 5)
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
                    comment="day|rsi_div|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
