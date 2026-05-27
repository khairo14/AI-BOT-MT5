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
    "divergence_lookback": 40,   # 40 M30 bars = 20 hours — captures real multi-session divergences
    "atr_period": 14,
    "sl_atr_mult": 1.5,          # SL = curr_close ± ATR × this (consistent with other strategies)
    "tp_rr": 1.5,               # tp1 (partial close)
    "tp2_rr": 2.5,              # tp2 (full close)
    "max_spread_pips": 2.0,     # gate for GOLD/GER40/GBPJPY wide-spread conditions
    "pivot_window": 2,
    "vol_confirm_mult": 1.1,
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

        # -- Swing pivot scanner (2-bar window each side) --
        def _swing_lows(s, w=2):
            idxs = []
            arr = s.to_numpy()
            for i in range(w, len(arr) - w):
                if all(arr[i] <= arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] <= arr[i+j] for j in range(1, w+1)):
                    idxs.append(i)
            return idxs

        def _swing_highs(s, w=2):
            idxs = []
            arr = s.to_numpy()
            for i in range(w, len(arr) - w):
                if all(arr[i] >= arr[i-j] for j in range(1, w+1)) and \
                all(arr[i] >= arr[i+j] for j in range(1, w+1)):
                    idxs.append(i)
            return idxs

        price_arr = price_slice.to_numpy()
        rsi_arr   = rsi_slice.to_numpy()

        low_pivots  = _swing_lows(price_slice)
        high_pivots = _swing_highs(price_slice)

        # Bullish: need at least 2 swing lows — most recent low < prior low, RSI reversed
        bullish_div = False
        if len(low_pivots) >= 2:
            i2, i1 = low_pivots[-1], low_pivots[-2]   # i2 is more recent
            if price_arr[i2] < price_arr[i1] and rsi_arr[i2] > rsi_arr[i1]:
                bullish_div = True

        # Bearish: need at least 2 swing highs — most recent high > prior high, RSI reversed
        bearish_div = False
        if len(high_pivots) >= 2:
            i2, i1 = high_pivots[-1], high_pivots[-2]
            if price_arr[i2] > price_arr[i1] and rsi_arr[i2] < rsi_arr[i1]:
                bearish_div = True

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

        # Confirmation: RSI crossed 50 in trade direction within the last 2 bars.
        # Requiring the cross on the exact current bar missed valid divergences where
        # the RSI-50 cross completed one bar prior but divergence is still fresh.
        prev2_rsi = rsi.iloc[-3] if len(rsi) >= 3 else prev_rsi
        rsi_cross_up   = (prev_rsi < 50 <= curr_rsi) or (prev2_rsi < 50 <= prev_rsi)
        rsi_cross_down = (prev_rsi > 50 >= curr_rsi) or (prev2_rsi > 50 >= prev_rsi)

        indicators = {
            "rsi": round(curr_rsi, 2),
            "rsi_cross_up": rsi_cross_up,
            "rsi_cross_down": rsi_cross_down,
            "bullish_div": bullish_div,
            "bearish_div": bearish_div,
            "ema_bias": round(curr_ema, 5),
            "atr": round(curr_atr, 5),
        }

        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.1)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult
                
        if bullish_div and rsi_cross_up and vol_ok:
            # SL is ATR-based from current entry price — anchoring to price_low_val
            # inflated sl_dist when price had already rallied far above the divergence
            # low by the time the RSI-50 cross fired, making TPs unrealistically far.
            sl_dist = curr_atr * p["sl_atr_mult"]
            sl_dist = max(sl_dist, self._min_sl_dist(curr_close))
            sl  = round(curr_close - sl_dist, 5)
            tp1 = round(curr_close + sl_dist * p["tp_rr"], 5)
            tp2 = round(curr_close + sl_dist * p["tp2_rr"], 5)
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
                    comment="day|rsi_div|buy",
                ),
                indicators=indicators,
            )

        if bearish_div and rsi_cross_down and vol_ok:
            sl_dist = curr_atr * p["sl_atr_mult"]
            sl_dist = max(sl_dist, self._min_sl_dist(curr_close))
            sl  = round(curr_close + sl_dist, 5)
            tp1 = round(curr_close - sl_dist * p["tp_rr"], 5)
            tp2 = round(curr_close - sl_dist * p["tp2_rr"], 5)
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
                    comment="day|rsi_div|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
