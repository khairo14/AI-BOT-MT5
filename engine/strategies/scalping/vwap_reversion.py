"""
S3 — VWAP Reversion
Timeframe: M1 entry (Stochastic), M5 context (RSI)
Symbols: EURUSD, GBPUSD, EURJPY, US100Cash
"""

from __future__ import annotations

import ta
import numpy as np
import pandas as pd

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "sigma_entry": 1.5,
    "sigma_sl": 2.5,
    "rsi_period": 14,
    "stoch_k": 5,
    "stoch_d": 3,
    "stoch_smooth": 3,
    "rsi_oversold": 28,
    "rsi_overbought": 72,
    "max_spread_pips": 4.0,      # raised from 2.5 — VWAP runs on M1 where spreads matter more
    # Minimum viable RR guard: if (VWAP - entry) / (entry - SL) < this, skip.
    # Prevents taking trades where the TP is so close that spread eats the reward.
    "min_rr_to_vwap": 0.8,
}


class VWAPReversion(BaseStrategy):

    name = "vwap_reversion"
    trading_type = "scalping"
    timeframe = "M1"

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < 30:
            return self._no_signal()

        # ---------------------------------------------------------------
        # VWAP (daily reset) — computed from the dataframe's session open
        # ---------------------------------------------------------------
        df = df.copy()
        df["date"] = df["time"].dt.date
        df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
        df["cum_tp_vol"] = df.groupby("date")["typical"].transform(
            lambda x: (x * df.loc[x.index, "volume"]).cumsum()
        )
        df["cum_vol"] = df.groupby("date")["volume"].transform("cumsum")
        df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)

        # Rolling std of typical price (daily) for deviation bands
        df["vwap_std"] = df.groupby("date")["typical"].transform(
            lambda x: x.expanding().std()
        )

        df["upper1"] = df["vwap"] + p["sigma_entry"] * df["vwap_std"]
        df["lower1"] = df["vwap"] - p["sigma_entry"] * df["vwap_std"]
        df["upper_sl"] = df["vwap"] + p["sigma_sl"] * df["vwap_std"]
        df["lower_sl"] = df["vwap"] - p["sigma_sl"] * df["vwap_std"]

        close = df["close"]
        high = df["high"]
        low = df["low"]

        rsi = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()
        stoch = ta.momentum.StochasticOscillator(
            high, low, close,
            window=p["stoch_k"],
            smooth_window=p["stoch_d"],
        )
        stoch_k = stoch.stoch()
        stoch_d = stoch.stoch_signal()

        curr = df.iloc[-1]
        prev_k = stoch_k.iloc[-2]
        prev_d = stoch_d.iloc[-2]
        curr_k = stoch_k.iloc[-1]
        curr_d = stoch_d.iloc[-1]
        curr_rsi = rsi.iloc[-1]
        curr_close = close.iloc[-1]

        indicators = {
            "vwap": round(curr["vwap"], 5) if not np.isnan(curr["vwap"]) else None,
            "vwap_std": round(curr["vwap_std"], 6) if not np.isnan(curr["vwap_std"]) else None,
            "upper1": round(curr["upper1"], 5),
            "lower1": round(curr["lower1"], 5),
            "rsi": round(curr_rsi, 2),
            "stoch_k": round(curr_k, 2),
            "stoch_d": round(curr_d, 2),
        }

        # Skip if VWAP std is invalid or session is too young (< 20 bars → std unreliable)
        session_bars = int((df["date"] == curr["date"]).sum())
        if np.isnan(curr["vwap"]) or np.isnan(curr["vwap_std"]) or curr["vwap_std"] == 0:
            return self._no_signal(indicators)
        if session_bars < 20:
            return self._no_signal(indicators)

        # BUY: price at lower deviation, RSI oversold, Stoch %K crosses above %D
        if (
            curr_close <= curr["lower1"]
            and curr_rsi < p["rsi_oversold"]
            and prev_k <= prev_d
            and curr_k > curr_d
        ):
            sl  = round(curr["lower_sl"], 5)
            tp1 = round(curr["vwap"], 5)      # partial close at VWAP
            tp2 = round(curr["upper1"], 5)    # runner to the opposite entry band
            risk = curr_close - sl
            reward_to_vwap = tp1 - curr_close
            # Skip if VWAP is too close — spread would eat the reward
            if risk <= 0 or (reward_to_vwap / risk) < p["min_rr_to_vwap"]:
                return self._no_signal(indicators)
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
                    comment="scalp|vwap_rev|buy",
                ),
                indicators=indicators,
            )

        # SELL: price at upper deviation, RSI overbought, Stoch %K crosses below %D
        if (
            curr_close >= curr["upper1"]
            and curr_rsi > p["rsi_overbought"]
            and prev_k >= prev_d
            and curr_k < curr_d
        ):
            sl  = round(curr["upper_sl"], 5)
            tp1 = round(curr["vwap"], 5)      # partial close at VWAP
            tp2 = round(curr["lower1"], 5)    # runner to the opposite entry band
            risk = sl - curr_close
            reward_to_vwap = curr_close - tp1
            # Skip if VWAP is too close — spread would eat the reward
            if risk <= 0 or (reward_to_vwap / risk) < p["min_rr_to_vwap"]:
                return self._no_signal(indicators)
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
                    comment="scalp|vwap_rev|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
