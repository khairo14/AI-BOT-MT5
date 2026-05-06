"""
S4 — Stochastic RSI Pullback (Trend Continuation)
Timeframe: M5
Regime: trending_bull, trending_bear

Logic:
  In a confirmed uptrend (EMA21 > EMA50 > EMA200 on M5), wait for price to
  pull back and StochRSI-K to dip below `stochrsi_oversold` (20), then cross
  back above it — buy the pullback within the trend.
  Mirror logic for downtrends (StochRSI-K crosses below `stochrsi_overbought`).

  This fills the gap left by ema_scalp (needs crossover) and bb_squeeze (needs
  squeeze pattern) — neither fires reliably in established trending regimes.
  stoch_rsi_pullback is designed specifically for trending markets and can fire
  multiple times per day.
"""

from __future__ import annotations

import ta
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "ema_fast": 21,
    "ema_mid": 50,
    "ema_slow": 200,
    "stochrsi_period": 14,
    "stochrsi_smooth_k": 3,
    "stochrsi_smooth_d": 3,
    "stochrsi_oversold": 20,
    "stochrsi_overbought": 80,
    "atr_period": 14,
    "sl_atr_mult": 1.5,
    "tp1_rr": 1.5,           # partial close at 1.5R
    "rr": 2.5,               # full close at 2.5R
    "max_spread_pips": 2.0,
    "vol_confirm_mult": 1.2,  # volume must exceed 20-bar avg × this (0 = disabled)
    "adx_period": 14,
    "adx_min": 18,
}


class StochRSIPullback(BaseStrategy):

    name = "stoch_rsi_pullback"
    trading_type = "scalping"
    timeframe = "M5"

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        min_bars = max(p["ema_slow"] + 5, p["stochrsi_period"] * 2 + 5)
        if len(df) < min_bars:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # Trend structure — EMA stack on M5
        ema_fast  = ta.trend.EMAIndicator(close, window=p["ema_fast"]).ema_indicator()
        ema_mid   = ta.trend.EMAIndicator(close, window=p["ema_mid"]).ema_indicator()
        ema_slow  = ta.trend.EMAIndicator(close, window=p["ema_slow"]).ema_indicator()

        adx_ind = ta.trend.ADXIndicator(high, low, close, window=p["adx_period"])
        curr_adx = adx_ind.adx().iloc[-1]

        adx_ok = (not pd.isna(curr_adx)) and (curr_adx >= p["adx_min"])

        if not adx_ok:
            return self._no_signal()

        curr_ema_fast = ema_fast.iloc[-1]
        curr_ema_mid  = ema_mid.iloc[-1]
        curr_ema_slow = ema_slow.iloc[-1]

        uptrend   = curr_ema_fast > curr_ema_mid > curr_ema_slow
        downtrend = curr_ema_fast < curr_ema_mid < curr_ema_slow

        if not uptrend and not downtrend:
            return self._no_signal()

        # StochRSI for pullback timing
        stoch = ta.momentum.StochRSIIndicator(
            close,
            window=p["stochrsi_period"],
            smooth1=p["stochrsi_smooth_k"],
            smooth2=p["stochrsi_smooth_d"],
        )
        stoch_k = stoch.stochrsi_k() * 100   # scale 0-100
        prev_k  = stoch_k.iloc[-2]
        curr_k  = stoch_k.iloc[-1]

        # ATR for SL sizing
        atr = ta.volatility.AverageTrueRange(
            high, low, close, window=p["atr_period"]
        ).average_true_range()
        curr_atr   = atr.iloc[-1]
        curr_close = close.iloc[-1]

        # Volume confirmation
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        indicators = {
            "ema_fast":  round(curr_ema_fast, 5),
            "ema_mid":   round(curr_ema_mid, 5),
            "ema_slow":  round(curr_ema_slow, 5),
            "stoch_k":   round(float(curr_k), 2),
            "uptrend":   bool(uptrend),
            "downtrend": bool(downtrend),
            "atr":       round(float(curr_atr), 5),
            "vol_ok":    bool(vol_ok),
            "adx":       round(float(curr_adx), 2) if not pd.isna(curr_adx) else None,
            "adx_ok":    bool(adx_ok),
        }

        sl_dist = curr_atr * p["sl_atr_mult"]

        # BUY: uptrend + StochRSI crosses up out of oversold
        stoch_cross_up = prev_k < p["stochrsi_oversold"] and curr_k >= p["stochrsi_oversold"]
        if uptrend and stoch_cross_up and vol_ok:
            sl  = round(curr_close - sl_dist, 5)
            tp1 = round(curr_close + sl_dist * p["tp1_rr"], 5)
            tp2 = round(curr_close + sl_dist * p["rr"], 5)
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
                    comment="scalp|stoch_rsi_pullback|buy",
                ),
                indicators=indicators,
            )

        # SELL: downtrend + StochRSI crosses down out of overbought
        stoch_cross_down = prev_k > p["stochrsi_overbought"] and curr_k <= p["stochrsi_overbought"]
        if downtrend and stoch_cross_down and vol_ok:
            sl  = round(curr_close + sl_dist, 5)
            tp1 = round(curr_close - sl_dist * p["tp1_rr"], 5)
            tp2 = round(curr_close - sl_dist * p["rr"], 5)
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
                    comment="scalp|stoch_rsi_pullback|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
