"""
W1 — EMA Trend Rider
3-timeframe EMA alignment: D1 / H4 / H1
ADX > 25 trend strength filter
Entry at pullback to H1 EMA 21
"""

from __future__ import annotations

import ta
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "ema_fast": 21,
    "ema_mid": 50,
    "ema_slow": 200,
    "adx_period": 14,
    "adx_threshold": 25,
    "atr_period": 14,
    "pullback_atr_tolerance": 0.3,
    "sl_atr_mult": 1.5,
    "tp_rr": 2.5,
}


class EMATrendRider(BaseStrategy):

    name = "ema_trend_rider"
    trading_type = "swing"
    timeframe = "H1"

    def calculate(
        self,
        df: pd.DataFrame,
        df_h4: pd.DataFrame | None = None,
        df_d1: pd.DataFrame | None = None,
    ) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < 210:
            return self._no_signal()

        close_h1 = df["close"]
        high_h1  = df["high"]
        low_h1   = df["low"]

        ema21_h1  = ta.trend.EMAIndicator(close_h1, window=p["ema_fast"]).ema_indicator()
        ema50_h1  = ta.trend.EMAIndicator(close_h1, window=p["ema_mid"]).ema_indicator()
        ema200_h1 = ta.trend.EMAIndicator(close_h1, window=p["ema_slow"]).ema_indicator()
        adx_ind   = ta.trend.ADXIndicator(high_h1, low_h1, close_h1, window=p["adx_period"])
        adx_val   = adx_ind.adx()
        atr       = ta.volatility.AverageTrueRange(high_h1, low_h1, close_h1, window=p["atr_period"]).average_true_range()

        curr_close  = close_h1.iloc[-1]
        curr_ema21  = ema21_h1.iloc[-1]
        curr_ema50  = ema50_h1.iloc[-1]
        curr_ema200 = ema200_h1.iloc[-1]
        curr_adx    = adx_val.iloc[-1]
        curr_atr    = atr.iloc[-1]

        # H4 bias — require at least fast > slow
        h4_bullish = h4_bearish = None
        if df_h4 is not None and len(df_h4) >= 55:
            c4 = df_h4["close"]
            ema21_h4 = ta.trend.EMAIndicator(c4, window=p["ema_fast"]).ema_indicator().iloc[-1]
            ema50_h4 = ta.trend.EMAIndicator(c4, window=p["ema_mid"]).ema_indicator().iloc[-1]
            h4_bullish = ema21_h4 > ema50_h4
            h4_bearish = ema21_h4 < ema50_h4

        # D1 bias
        d1_bullish = d1_bearish = None
        if df_d1 is not None and len(df_d1) >= 55:
            cd = df_d1["close"]
            ema21_d1 = ta.trend.EMAIndicator(cd, window=p["ema_fast"]).ema_indicator().iloc[-1]
            ema50_d1 = ta.trend.EMAIndicator(cd, window=p["ema_mid"]).ema_indicator().iloc[-1]
            d1_bullish = ema21_d1 > ema50_d1
            d1_bearish = ema21_d1 < ema50_d1

        h1_bullish = curr_ema21 > curr_ema50 > curr_ema200
        h1_bearish = curr_ema21 < curr_ema50 < curr_ema200

        adx_ok = curr_adx >= p["adx_threshold"]
        tol    = curr_atr * p["pullback_atr_tolerance"]

        # Pullback to EMA21 — price within tolerance of the EMA
        at_ema_pull = abs(curr_close - curr_ema21) <= tol

        bull_aligned = h1_bullish and (h4_bullish is None or h4_bullish) and (d1_bullish is None or d1_bullish)
        bear_aligned = h1_bearish and (h4_bearish is None or h4_bearish) and (d1_bearish is None or d1_bearish)

        indicators = {
            "ema21_h1": round(curr_ema21, 5),
            "ema50_h1": round(curr_ema50, 5),
            "ema200_h1": round(curr_ema200, 5),
            "adx": round(curr_adx, 2),
            "at_ema_pull": at_ema_pull,
            "h4_bullish": h4_bullish,
            "d1_bullish": d1_bullish,
        }

        if bull_aligned and adx_ok and at_ema_pull and curr_close > curr_ema21:
            sl = round(low_h1.iloc[-5:].min() - curr_atr * p["sl_atr_mult"] * 0.2, 5)
            if self._sl_too_close("BUY", curr_close, sl):
                return self._no_signal(indicators)
            sl_dist = abs(curr_close - sl)
            tp = round(curr_close + sl_dist * p["tp_rr"], 5)
            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="swing|ema_trend|buy",
                ),
                indicators=indicators,
            )

        if bear_aligned and adx_ok and at_ema_pull and curr_close < curr_ema21:
            sl = round(high_h1.iloc[-5:].max() + curr_atr * p["sl_atr_mult"] * 0.2, 5)
            if self._sl_too_close("SELL", curr_close, sl):
                return self._no_signal(indicators)
            sl_dist = abs(sl - curr_close)
            tp = round(curr_close - sl_dist * p["tp_rr"], 5)
            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="swing|ema_trend|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
