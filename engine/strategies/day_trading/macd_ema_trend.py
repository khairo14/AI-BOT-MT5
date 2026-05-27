"""
D1 — MACD + EMA Trend Following
Timeframe: H1 (signal), M15 (entry)
Symbols: EURUSD, GBPUSD, GOLD, US100Cash, US30Cash, TSLA, NVDA
"""

from __future__ import annotations

import ta
import pandas as pd
from loguru import logger

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "ema_fast": 20,
    "ema_slow": 50,
    "ema_bias": 200,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "atr_period": 14,
    "sl_atr_mult": 1.2,           # SL = max(ATR × this, |close − EMA20| × 1.5)
    "max_entry_atr_dist": 1.3,    # skip if price is already >N×ATR from EMA20 (stale bounce)
    "tp1_rr": 1.5,   # must be ≥ risk_reward_min 1.5 (day_trading)
    "tp2_rr": 2.5,
    "vol_confirm_mult": 1.2,  # volume must exceed 20-bar avg × this (0 = disabled)
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

            # Require histogram positive/negative — remove the "growing" bar-over-bar
            # requirement which was too strict and filtered valid entries in steady trends.
            h1_macd_bull = h1_hist.iloc[-1] > 0
            h1_macd_bear = h1_hist.iloc[-1] < 0
        else:
            # IMPROVE-5: log when H1 data is absent so systematic fetch failures are visible
            logger.debug(
                f"macd_ema_trend/{self.symbol}: H1 data unavailable "
                f"(df_h1={'None' if df_h1 is None else f'only {len(df_h1)} bars'}) "
                "— strategy inactive this tick"
            )

        curr_close = close.iloc[-1]
        curr_ema20_m15 = ema_fast_m15.iloc[-1]
        curr_atr = atr.iloc[-1]

        # Proximity filter: skip if price has drifted more than max_entry_atr_dist×ATR
        # away from EMA20. Prevents entering a stale bounce (e.g. crossover 5 bars ago
        # and price has already moved 2×ATR) which inflates the SL and degrades R:R.
        max_dist = curr_atr * p.get("max_entry_atr_dist", 1.0)
        near_ema = abs(curr_close - curr_ema20_m15) <= max_dist

        # Pullback bounce on M15: price crossed EMA20 within the last 6 bars (90 min).
        # A 6-bar window covers the full preceding H1 bar so bounces that complete
        # early in the session are not missed when the runner fires at H1 close.
        bull_bounce = any(
            close.iloc[i - 1] <= ema_fast_m15.iloc[i - 1] and close.iloc[i] > ema_fast_m15.iloc[i]
            for i in range(-6, 0)
        )
        bear_bounce = any(
            close.iloc[i - 1] >= ema_fast_m15.iloc[i - 1] and close.iloc[i] < ema_fast_m15.iloc[i]
            for i in range(-6, 0)
        )

        indicators = {
            "h1_trend": h1_trend,
            "h1_macd_bull": h1_macd_bull,
            "h1_macd_bear": h1_macd_bear,
            "m15_ema20": round(curr_ema20_m15, 5),
            "atr": round(curr_atr, 5),
            "bull_bounce": bull_bounce,
            "bear_bounce": bear_bounce,
            "curr_close": round(curr_close, 5),
            "near_ema": near_ema,
        }

        # Volume confirmation on entry bar (M15 primary timeframe)
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        if h1_trend == "BULL" and h1_macd_bull and bull_bounce and near_ema and vol_ok:
            sl_dist = max(curr_atr * p["sl_atr_mult"], abs(curr_close - curr_ema20_m15) * 1.5)
            sl  = round(curr_close - sl_dist, 5)
            tp1 = round(curr_close + sl_dist * p["tp1_rr"], 5)
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
                    comment="day|macd_ema|buy",
                ),
                indicators=indicators,
            )

        if h1_trend == "BEAR" and h1_macd_bear and bear_bounce and near_ema and vol_ok:
            sl_dist = max(curr_atr * p["sl_atr_mult"], abs(curr_close - curr_ema20_m15) * 1.5)
            sl  = round(curr_close + sl_dist, 5)
            tp1 = round(curr_close - sl_dist * p["tp1_rr"], 5)
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
                    comment="day|macd_ema|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
