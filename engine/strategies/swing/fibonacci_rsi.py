"""
W2 — Fibonacci RSI
Auto-detects H4 impulse moves, computes Fibonacci retracement levels
Entry in the golden zone (50–61.8%), confirmed by RSI range + candle close
"""

from __future__ import annotations

import ta
import pandas as pd
import numpy as np

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "impulse_lookback": 50,
    "fib_entry_low": 0.50,
    "fib_entry_high": 0.618,
    "fib_tp": 1.0,       # TP at 100% swing retracement (full extension) — used as tp2
    "tp1_rr": 1.5,       # tp1 at 1.5R (partial close before swinging to full target)
    "rsi_period": 14,
    "rsi_bull_min": 35,
    "rsi_bull_max": 65,
    "rsi_bear_min": 35,
    "rsi_bear_max": 65,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "candle_confirm": True,
}

FIB_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


def _compute_fibs(swing_low: float, swing_high: float) -> dict[float, float]:
    rng = swing_high - swing_low
    return {lvl: round(swing_high - lvl * rng, 5) for lvl in FIB_LEVELS}


class FibonacciRSI(BaseStrategy):

    name = "fibonacci_rsi"
    trading_type = "swing"
    timeframe = "H4"

    def calculate(self, df: pd.DataFrame) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < p["impulse_lookback"] + 10:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        rsi = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()
        atr = ta.volatility.AverageTrueRange(high, low, close, window=p["atr_period"]).average_true_range()

        lb = p["impulse_lookback"]
        window_high = df["high"].iloc[-lb:]
        window_low  = df["low"].iloc[-lb:]

        swing_high_idx = window_high.idxmax()
        swing_low_idx  = window_low.idxmin()

        swing_high = window_high.max()
        swing_low  = window_low.min()

        curr_close = close.iloc[-1]
        curr_rsi   = rsi.iloc[-1]
        curr_atr   = atr.iloc[-1]

        # Determine impulse direction based on which extreme came first
        # Bullish impulse: low formed before high → retracement pulls back down into golden zone
        # Bearish impulse: high formed before low → retracement bounces up into golden zone

        try:
            high_pos = int(df.index.get_loc(swing_high_idx))
        except (KeyError, TypeError, ValueError):
            high_pos = -1
        try:
            low_pos = int(df.index.get_loc(swing_low_idx))
        except (KeyError, TypeError, ValueError):
            low_pos = -1

        fibs_bull = _compute_fibs(swing_low, swing_high)   # retracement of up-move
        fibs_bear = _compute_fibs(swing_high, swing_low)   # retracement of down-move (inverted)

        golden_high_bull = fibs_bull[p["fib_entry_low"]]   # 50.0% level of up-move (higher price)
        golden_low_bull  = fibs_bull[p["fib_entry_high"]]  # 61.8% level (lower price)

        golden_low_bear  = fibs_bear[p["fib_entry_low"]]   # 50.0% retracement of down-move
        golden_high_bear = fibs_bear[p["fib_entry_high"]]  # 61.8% retracement (higher price)

        # Bullish setup: impulse was up, price now pulling back into golden zone
        bullish_setup = (
            high_pos > low_pos                           # low came first (up-move)
            and golden_low_bull <= curr_close <= golden_high_bull
            and p["rsi_bull_min"] <= curr_rsi <= p["rsi_bull_max"]
        )

        # Bearish setup: impulse was down, price now bouncing into golden zone
        bearish_setup = (
            low_pos > high_pos                           # high came first (down-move)
            and golden_low_bear <= curr_close <= golden_high_bear
            and p["rsi_bear_min"] <= curr_rsi <= p["rsi_bear_max"]
        )

        # Candle confirmation
        prev_close = close.iloc[-2]
        prev_open  = df["open"].iloc[-2]
        curr_open  = df["open"].iloc[-1]

        bull_candle = curr_close > curr_open and curr_close > prev_close  # bullish closing candle
        bear_candle = curr_close < curr_open and curr_close < prev_close  # bearish closing candle

        indicators = {
            "rsi": round(curr_rsi, 2),
            "swing_high": round(swing_high, 5),
            "swing_low": round(swing_low, 5),
            "golden_zone_bull": (round(golden_low_bull, 5), round(golden_high_bull, 5)),
            "golden_zone_bear": (round(golden_low_bear, 5), round(golden_high_bear, 5)),
            "bullish_setup": bullish_setup,
            "bearish_setup": bearish_setup,
        }

        if bullish_setup and (not p["candle_confirm"] or bull_candle):
            sl = round(curr_close - curr_atr * p["sl_atr_mult"], 5)
            if self._sl_too_close("BUY", curr_close, sl):
                return self._no_signal(indicators)
            sl_dist = abs(curr_close - sl)
            tp1 = round(curr_close + sl_dist * p["tp1_rr"], 5)
            tp2 = round(swing_high, 5)  # tp2 = 100% extension (origin swing high)
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
                    comment="swing|fib_rsi|buy",
                ),
                indicators=indicators,
            )

        if bearish_setup and (not p["candle_confirm"] or bear_candle):
            sl = round(curr_close + curr_atr * p["sl_atr_mult"], 5)
            if self._sl_too_close("SELL", curr_close, sl):
                return self._no_signal(indicators)
            sl_dist = abs(sl - curr_close)
            tp1 = round(curr_close - sl_dist * p["tp1_rr"], 5)
            tp2 = round(swing_low, 5)   # tp2 = 100% (origin low)
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
                    comment="swing|fib_rsi|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
