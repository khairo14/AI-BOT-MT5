"""
S1 — EMA Scalp (Trend Following)
Timeframe: M1/M2 entry, M5 bias filter
Symbols: EURUSD, GBPUSD, USDJPY, EURJPY, USDCHF
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
    "rsi_max": 65,
    "max_spread_pips": 1.5,
    "sl_pips": 4,
    "rr": 2.0,
}


class EMAScalp(BaseStrategy):

    name = "ema_scalp"
    trading_type = "scalping"
    timeframe = "M1"

    def calculate(self, df: pd.DataFrame, df_m5: pd.DataFrame | None = None) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < p["ema_slow"] + 5:
            return self._no_signal()

        close = df["close"]

        # --- Indicators ---
        ema_fast = ta.trend.EMAIndicator(close, window=p["ema_fast"]).ema_indicator()
        ema_slow = ta.trend.EMAIndicator(close, window=p["ema_slow"]).ema_indicator()
        rsi = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()

        # M5 bias: price above/below EMA 50 on M5
        bias = "NONE"
        if df_m5 is not None and len(df_m5) >= p["ema_bias_period"]:
            ema_bias = ta.trend.EMAIndicator(
                df_m5["close"], window=p["ema_bias_period"]
            ).ema_indicator()
            last_m5_close = df_m5["close"].iloc[-1]
            last_m5_ema = ema_bias.iloc[-1]
            bias = "BULL" if last_m5_close > last_m5_ema else "BEAR"

        prev_fast = ema_fast.iloc[-2]
        prev_slow = ema_slow.iloc[-2]
        curr_fast = ema_fast.iloc[-1]
        curr_slow = ema_slow.iloc[-1]
        curr_rsi = rsi.iloc[-1]
        curr_close = close.iloc[-1]

        indicators = {
            "ema_fast": round(curr_fast, 5),
            "ema_slow": round(curr_slow, 5),
            "rsi": round(curr_rsi, 2),
            "m5_bias": bias,
        }

        # --- BUY: EMA fast crosses above slow, bias bullish, RSI in range ---
        if (
            prev_fast <= prev_slow
            and curr_fast > curr_slow
            and (bias == "BULL" or bias == "NONE")
            and p["rsi_min"] <= curr_rsi <= p["rsi_max"]
        ):
            pip = self._pip_size()
            sl = round(curr_close - p["sl_pips"] * pip, 5)
            tp = round(curr_close + p["sl_pips"] * p["rr"] * pip, 5)
            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="ema_scalp|buy",
                ),
                indicators=indicators,
            )

        # --- SELL: EMA fast crosses below slow, bias bearish, RSI in range ---
        sell_rsi_min = 100 - p["rsi_max"]
        sell_rsi_max = 100 - p["rsi_min"]
        if (
            prev_fast >= prev_slow
            and curr_fast < curr_slow
            and (bias == "BEAR" or bias == "NONE")
            and sell_rsi_min <= curr_rsi <= sell_rsi_max
        ):
            pip = self._pip_size()
            sl = round(curr_close + p["sl_pips"] * pip, 5)
            tp = round(curr_close - p["sl_pips"] * p["rr"] * pip, 5)
            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="scalp|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)

    def _pip_size(self) -> float:
        """Return pip size based on symbol suffix."""
        jpy_pairs = ("JPY", "HUF", "SEK", "NOK", "DKK")
        if any(self.symbol.upper().endswith(s) for s in jpy_pairs):
            return 0.01
        return 0.0001
