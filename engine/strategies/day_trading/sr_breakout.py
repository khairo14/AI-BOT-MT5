"""
D2 — Support / Resistance Breakout
Timeframe: H1
Symbols: GOLD, OilCash, US500Cash, BTCUSD, ETHUSD
"""

from __future__ import annotations

import ta
import pandas as pd
import numpy as np

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "lookback_bars": 50,
    "atr_period": 14,
    "rsi_period": 14,
    "retest_mode": True,
    "sl_buffer_atr": 0.3,
    "tp_rr": 1.8,
}


class SRBreakout(BaseStrategy):

    name = "sr_breakout"
    trading_type = "day_trading"
    timeframe = "H1"

    def calculate(self, df: pd.DataFrame, **_) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        lookback = p["lookback_bars"]
        if len(df) < lookback + 10:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        atr_series = ta.volatility.AverageTrueRange(
            high, low, close, window=p["atr_period"]
        ).average_true_range()
        rsi_series = ta.momentum.RSIIndicator(close, window=p["rsi_period"]).rsi()

        curr_close = close.iloc[-1]
        curr_atr = atr_series.iloc[-1]
        curr_rsi = rsi_series.iloc[-1]
        avg_atr = atr_series.iloc[-lookback:].mean()

        # Detect S/R levels using fractal highs/lows in lookback window
        window_high = high.iloc[-lookback:-1]
        window_low  = low.iloc[-lookback:-1]
        resistance = self._find_level(window_high, "high")
        support    = self._find_level(window_low, "low")

        indicators = {
            "resistance": round(resistance, 5) if resistance else None,
            "support":    round(support, 5) if support else None,
            "rsi":        round(curr_rsi, 2),
            "atr":        round(curr_atr, 5),
            "avg_atr":    round(avg_atr, 5),
        }

        # Only trade breakouts when volatility is at least average
        if curr_atr < avg_atr * 0.8:
            return self._no_signal(indicators)

        buffer = curr_atr * p["sl_buffer_atr"]

        # Bullish breakout: close breaks above resistance
        if resistance and curr_close > resistance and curr_rsi > 55:
            if p["retest_mode"]:
                # In retest mode: signal fires when price comes back to retest the broken level
                prev_close = close.iloc[-2]
                if not (prev_close < resistance and curr_close > (resistance - buffer)):
                    return self._no_signal(indicators)

            sl  = round(resistance - buffer, 5)
            next_res = self._find_next_level(high.iloc[-lookback:], resistance, "up")
            tp = round(next_res if next_res else curr_close + curr_atr * p["tp_rr"], 5)

            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="day|sr_break|buy",
                ),
                indicators=indicators,
            )

        # Bearish breakout: close breaks below support
        if support and curr_close < support and curr_rsi < 45:
            if p["retest_mode"]:
                prev_close = close.iloc[-2]
                if not (prev_close > support and curr_close < (support + buffer)):
                    return self._no_signal(indicators)

            sl  = round(support + buffer, 5)
            next_sup = self._find_next_level(low.iloc[-lookback:], support, "down")
            tp = round(next_sup if next_sup else curr_close - curr_atr * p["tp_rr"], 5)

            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=curr_close,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="day|sr_break|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_level(series: pd.Series, kind: str) -> float | None:
        """Return the most significant fractal high or low in the series."""
        if kind == "high":
            # Cluster near the maximum
            peak = series.max()
            cluster = series[series >= peak * 0.9995]
            return float(cluster.mean()) if len(cluster) >= 2 else float(peak)
        else:
            trough = series.min()
            cluster = series[series <= trough * 1.0005]
            return float(cluster.mean()) if len(cluster) >= 2 else float(trough)

    @staticmethod
    def _find_next_level(series: pd.Series, current_level: float, direction: str) -> float | None:
        """Find the next S/R level beyond the current breakout level."""
        if direction == "up":
            candidates = series[series > current_level * 1.001]
            return float(candidates.min()) if len(candidates) else None
        else:
            candidates = series[series < current_level * 0.999]
            return float(candidates.max()) if len(candidates) else None
