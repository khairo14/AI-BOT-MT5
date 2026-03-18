"""
D1 — MACD + EMA Trend Following
Timeframe: H1 (signal), M15 (entry)
Symbols: EURUSD, GBPUSD, GOLD, US100Cash, US30Cash, TSLA, NVDA
"""

from __future__ import annotations

import ta
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_bias": 200,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "atr_period": 14,
    "tp1_rr": 1.0,
    "tp2_rr": 2.0,
}


class MACDEMATrend(BaseStrategy):

    name = "macd_ema_trend"
    trading_type = "day_trading"
    timeframe = "M15"

    def calculate(self, df: pd.DataFrame, df_h1: pd.DataFrame | None = None) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < 30:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # M15 indicators for entry
        ema_fast_m15 = ta.trend.EMAIndicator(close, window=p["ema_fast"]).ema_indicator()
        atr = ta.volatility.AverageTrueRange(high, low, close, window=p["atr_period"]).average_true_range()

        # H1 indicators for trend/signal confirmation
        h1_trend = "NONE"
        h1_macd_bull = False
        h1_macd_bear = False

        if df_h1 is not None and len(df_h1) >= p["ema_bias"] + 5:
            h1_close = df_h1["close"]
            h1_ema20 = ta.trend.EMAIndicator(h1_close, window=p["ema_fast"]).ema_indicator()
            h1_ema50 = ta.trend.EMAIndicator(h1_close, window=p["ema_slow"]).ema_indicator()
            h1_ema200 = ta.trend.EMAIndicator(h1_close, window=p["ema_bias"]).ema_indicator()
            macd_obj = ta.trend.MACD(
                h1_close,
                window_fast=p["macd_fast"],
                window_slow=p["macd_slow"],
                window_sign=p["macd_signal"],
            )
            h1_hist = macd_obj.macd_diff()

            last_h1 = h1_close.iloc[-1]
            if last_h1 > h1_ema200.iloc[-1] and h1_ema20.iloc[-1] > h1_ema50.iloc[-1]:
                h1_trend = "BULL"
            elif last_h1 < h1_ema200.iloc[-1] and h1_ema20.iloc[-1] < h1_ema50.iloc[-1]:
                h1_trend = "BEAR"

            h1_macd_bull = h1_hist.iloc[-1] > 0 and h1_hist.iloc[-1] > h1_hist.iloc[-2]
            h1_macd_bear = h1_hist.iloc[-1] < 0 and h1_hist.iloc[-1] < h1_hist.iloc[-2]

        curr_close = close.iloc[-1]
        prev_close = close.iloc[-2]
        curr_ema20_m15 = ema_fast_m15.iloc[-1]
        prev_ema20_m15 = ema_fast_m15.iloc[-2]
        curr_atr = atr.iloc[-1]

        # Pullback bounce on M15: price was at/below EMA20 last candle, now closes above it
        bull_bounce = prev_close <= prev_ema20_m15 and curr_close > curr_ema20_m15
        bear_bounce = prev_close >= prev_ema20_m15 and curr_close < curr_ema20_m15

        indicators = {
            "h1_trend": h1_trend,
            "h1_macd_bull": h1_macd_bull,
            "h1_macd_bear": h1_macd_bear,
            "m15_ema20": round(curr_ema20_m15, 5),
            "atr": round(curr_atr, 5),
            "bull_bounce": bull_bounce,
            "bear_bounce": bear_bounce,
        }

        if h1_trend == "BULL" and h1_macd_bull and bull_bounce:
            sl_dist = max(curr_atr * 1.2, abs(curr_close - curr_ema20_m15) * 1.5)
            sl  = round(curr_close - sl_dist, 5)
            tp1 = round(curr_close + sl_dist * p["tp1_rr"], 5)
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
                    comment="day|macd_ema|buy",
                ),
                indicators=indicators,
            )

        if h1_trend == "BEAR" and h1_macd_bear and bear_bounce:
            sl_dist = max(curr_atr * 1.2, abs(curr_close - curr_ema20_m15) * 1.5)
            sl  = round(curr_close + sl_dist, 5)
            tp1 = round(curr_close - sl_dist * p["tp1_rr"], 5)
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
                    comment="day|macd_ema|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
