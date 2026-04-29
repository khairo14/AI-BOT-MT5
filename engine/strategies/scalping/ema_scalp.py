"""
S1 — EMA Scalp (Trend Following)
Timeframe: M5 entry, M15 bias filter
Symbols: any forex (majors, minors, exotics), commodities, indices

SL is ATR-based so the strategy adapts to each symbol's actual volatility
rather than a fixed pip count that was too tight for exotics and commodities.
ADX filter blocks entries in choppy/ranging conditions where EMA crossovers
are pure noise. EMA separation filter ensures the crossover has real momentum.
"""

from __future__ import annotations

import ta
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "ema_fast": 8,
    "ema_slow": 21,
    "ema_bias_period": 50,
    "rsi_period": 7,
    "rsi_min": 40,
    "rsi_max": 72,
    "max_spread_pips": 4.0,      # covers exotics & commodities (XM: AUDCAD~2pip, XAUUSD~3pip, exotics~5-8pip)
    # ATR-based SL — replaces fixed sl_pips so the strategy scales to any symbol
    "atr_period": 14,
    "atr_sl_mult": 1.5,          # SL = ATR × this; optimizer searches [1.0, 1.5, 2.0, 2.5]
    "tp1_rr": 1.2,               # partial close at 1.2R
    "rr": 2.0,                   # tp2 (full close) at 2.0R
    "vol_confirm_mult": 1.3,     # volume must exceed 50-bar avg × this (0 = disabled)
    "crossover_window": 3,       # look back N bars for a valid crossover
    # ADX trend-strength filter — blocks entries in choppy/ranging conditions
    "adx_period": 14,
    "adx_min": 20,               # require ADX > this before allowing entry
    # EMA separation filter — crossover must have real momentum, not just noise
    # Minimum gap between fast and slow EMA expressed as ATR fraction
    "ema_sep_mult": 0.1,         # require |ema_fast - ema_slow| > ATR × this
}


class EMAScalp(BaseStrategy):

    name = "ema_scalp"
    trading_type = "scalping"
    timeframe = "M5"

    def calculate(self, df: pd.DataFrame, df_m15: pd.DataFrame | None = None) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        min_bars = max(p["ema_slow"], p["atr_period"], p["adx_period"]) + 10
        if len(df) < min_bars:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        # --- Indicators ---
        ema_fast = ta.trend.EMAIndicator(close, window=p["ema_fast"]).ema_indicator()
        ema_slow = ta.trend.EMAIndicator(close, window=p["ema_slow"]).ema_indicator()
        rsi      = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()

        # ATR — used for SL sizing and EMA separation filter
        atr = ta.volatility.AverageTrueRange(
            high, low, close, window=p["atr_period"]
        ).average_true_range()
        curr_atr = atr.iloc[-1]
        if curr_atr <= 0 or pd.isna(curr_atr):
            return self._no_signal()

        # ADX — trend strength filter
        adx_ind  = ta.trend.ADXIndicator(high, low, close, window=p["adx_period"])
        curr_adx = adx_ind.adx().iloc[-1]

        # M15 bias: price above/below EMA 50 on M15
        bias = "NONE"
        if df_m15 is not None and len(df_m15) >= p["ema_bias_period"]:
            ema_bias = ta.trend.EMAIndicator(
                df_m15["close"], window=p["ema_bias_period"]
            ).ema_indicator()
            last_m15_close = df_m15["close"].iloc[-1]
            last_m15_ema   = ema_bias.iloc[-1]
            bias = "BULL" if last_m15_close > last_m15_ema else "BEAR"

        curr_fast  = ema_fast.iloc[-1]
        curr_slow  = ema_slow.iloc[-1]
        curr_rsi   = rsi.iloc[-1]
        curr_close = close.iloc[-1]

        # ADX gate — block entries when market is too choppy
        adx_ok = (not pd.isna(curr_adx)) and (curr_adx >= p["adx_min"])

        # EMA separation gate — crossover must have real momentum
        ema_sep_ok = abs(curr_fast - curr_slow) >= curr_atr * p["ema_sep_mult"]

        # Crossover detection over a rolling window
        window = max(1, int(p.get("crossover_window", 3)))
        bull_cross = False
        bear_cross = False
        for _i in range(-window, 0):
            _pf = ema_fast.iloc[_i - 1]
            _ps = ema_slow.iloc[_i - 1]
            _cf = ema_fast.iloc[_i]
            _cs = ema_slow.iloc[_i]
            if _pf <= _ps and _cf > _cs:
                bull_cross = True
            if _pf >= _ps and _cf < _cs:
                bear_cross = True
        # Price must still be on the correct side of both EMAs at the current bar
        bull_cross = bull_cross and curr_fast > curr_slow
        bear_cross = bear_cross and curr_fast < curr_slow

        # Volume confirmation — 50-bar baseline (4h at M5) avoids false signals
        # from the natural session rhythm that a 20-bar window would catch.
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.3)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 51:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-51:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        # ATR-based SL distance — adapts to every symbol's actual volatility
        sl_dist = curr_atr * p["atr_sl_mult"]
        # Enforce broker minimum SL distance
        sl_dist = max(sl_dist, self._min_sl_dist(curr_close))

        indicators = {
            "ema_fast":   round(curr_fast, 5),
            "ema_slow":   round(curr_slow, 5),
            "rsi":        round(curr_rsi, 2),
            "atr":        round(curr_atr, 5),
            "adx":        round(curr_adx, 2) if not pd.isna(curr_adx) else None,
            "adx_ok":     adx_ok,
            "ema_sep_ok": ema_sep_ok,
            "m15_bias":   bias,
            "vol_ok":     vol_ok,
        }

        # --- BUY ---
        if (
            bull_cross
            and bias == "BULL"
            and p["rsi_min"] <= curr_rsi <= p["rsi_max"]
            and adx_ok
            and ema_sep_ok
            and vol_ok
        ):
            sl  = round(curr_close - sl_dist, 5)
            tp1 = round(curr_close + sl_dist * p["tp1_rr"], 5)
            tp2 = round(curr_close + sl_dist * p["rr"], 5)
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
                    comment="scalp|ema_scalp|buy",
                ),
                indicators=indicators,
            )

        # --- SELL ---
        sell_rsi_min = 100 - p["rsi_max"]
        sell_rsi_max = 100 - p["rsi_min"]
        if (
            bear_cross
            and bias == "BEAR"
            and sell_rsi_min <= curr_rsi <= sell_rsi_max
            and adx_ok
            and ema_sep_ok
            and vol_ok
        ):
            sl  = round(curr_close + sl_dist, 5)
            tp1 = round(curr_close - sl_dist * p["tp1_rr"], 5)
            tp2 = round(curr_close - sl_dist * p["rr"], 5)
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
                    comment="scalp|ema_scalp|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
