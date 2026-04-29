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
    "retest_mode": False,
    "sl_buffer_atr": 0.3,
    "tp1_rr": 1.5,   # partial close at 1.5R
    "tp_rr": 2.5,    # tp2 runner — raised from 1.8 (only 0.3R gap) to 2.5 for meaningful runner
    "min_touches": 3,          # raised from 2 — 3 touches = more significant structural level
    "level_tolerance": 0.0015, # price tolerance for touch counting (0.15%)
    "vol_confirm_mult": 1.2,   # volume must exceed 20-bar avg × this factor (0 = disabled)
    "max_spread_pips": 2.0,    # blocks GOLD/oil/BTC during wide-spread conditions
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

        # Detect S/R levels using fractal multi-touch counting in lookback window
        window_high = high.iloc[-lookback:-1]
        window_low  = low.iloc[-lookback:-1]
        resistance = self._find_level(
            window_high, "high",
            min_touches=p["min_touches"],
            tolerance=p["level_tolerance"],
        )
        support = self._find_level(
            window_low, "low",
            min_touches=p["min_touches"],
            tolerance=p["level_tolerance"],
        )

        # Volume confirmation: breakout bar must show expanded volume
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        indicators = {
            "resistance":  round(resistance, 5) if resistance else None,
            "support":     round(support, 5) if support else None,
            "rsi":         round(curr_rsi, 2),
            "atr":         round(curr_atr, 5),
            "avg_atr":     round(avg_atr, 5),
            "vol_ok":      vol_ok,
        }

        # Only trade breakouts when volatility is at least average
        if curr_atr < avg_atr * 0.8:
            return self._no_signal(indicators)

        buffer = curr_atr * p["sl_buffer_atr"]

        # Bullish breakout: close breaks above resistance
        if resistance and curr_close > resistance and curr_rsi > 55 and vol_ok:
            if p["retest_mode"]:
                # In retest mode: signal fires when price comes back to retest the broken level
                prev_close = close.iloc[-2]
                if not (prev_close < resistance and curr_close > (resistance - buffer)):
                    return self._no_signal(indicators)

            sl_dist = abs(curr_close - (resistance - buffer))
            sl   = round(resistance - buffer, 5)
            tp1  = round(curr_close + sl_dist * p["tp1_rr"], 5)
            next_res = self._find_next_level(high.iloc[-lookback:], resistance, "up")
            # tp2 must be strictly beyond tp1; if the next structural level is closer
            # than tp1 (or not found), fall back to the ATR-based tp_rr distance.
            _tp2_candidate = next_res if (next_res and next_res > tp1) else None
            tp2  = round(_tp2_candidate if _tp2_candidate else curr_close + curr_atr * p["tp_rr"], 5)
            # Final guard: tp2 must at minimum equal tp_rr × sl_dist beyond entry
            _tp2_min = round(curr_close + sl_dist * p["tp_rr"], 5)
            if tp2 < _tp2_min:
                tp2 = _tp2_min

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
                    comment="day|sr_break|buy",
                ),
                indicators=indicators,
            )

        # Bearish breakout: close breaks below support
        if support and curr_close < support and curr_rsi < 45 and vol_ok:
            if p["retest_mode"]:
                prev_close = close.iloc[-2]
                if not (prev_close > support and curr_close < (support + buffer)):
                    return self._no_signal(indicators)

            sl_dist = abs((support + buffer) - curr_close)
            sl   = round(support + buffer, 5)
            tp1  = round(curr_close - sl_dist * p["tp1_rr"], 5)
            next_sup = self._find_next_level(low.iloc[-lookback:], support, "down")
            # tp2 must be strictly beyond tp1 (lower for SELL); if the next structural
            # level is closer than tp1 (or not found), fall back to ATR-based tp_rr.
            _tp2_candidate = next_sup if (next_sup and next_sup < tp1) else None
            tp2  = round(_tp2_candidate if _tp2_candidate else curr_close - curr_atr * p["tp_rr"], 5)
            # Final guard: tp2 must at minimum equal tp_rr × sl_dist beyond entry
            _tp2_min = round(curr_close - sl_dist * p["tp_rr"], 5)
            if tp2 > _tp2_min:
                tp2 = _tp2_min

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
                    comment="day|sr_break|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _find_level(
        series: pd.Series,
        kind: str,
        min_touches: int = 2,
        tolerance: float = 0.0015,
    ) -> float | None:
        """
        Identify the most significant S/R level using fractal pivot counting.

        1.  Find fractal highs/lows (local extrema with a 2-bar window).
        2.  For each pivot, count how many bars in the full series touched
            the same price zone (within ±tolerance).
        3.  Return the pivot whose zone was touched the most times, provided
            it meets the min_touches threshold.  Falls back to the simple
            max/min when no multi-touch level is found.
        """
        if len(series) < 5:
            return None

        arr = series.to_numpy()

        # Step 1 – detect fractal pivots (strict 2-bar window each side)
        pivots: list[float] = []
        for i in range(2, len(arr) - 2):
            if kind == "high":
                if arr[i] >= arr[i-1] and arr[i] >= arr[i-2] and arr[i] >= arr[i+1] and arr[i] >= arr[i+2]:
                    pivots.append(float(arr[i]))
            else:
                if arr[i] <= arr[i-1] and arr[i] <= arr[i-2] and arr[i] <= arr[i+1] and arr[i] <= arr[i+2]:
                    pivots.append(float(arr[i]))

        if not pivots:
            return float(arr.max() if kind == "high" else arr.min())

        # Step 2 – count touches per pivot across the full series
        best_level: float | None = None
        best_count = 0

        for pivot in pivots:
            band_lo = pivot * (1.0 - tolerance)
            band_hi = pivot * (1.0 + tolerance)
            count = int(np.sum((arr >= band_lo) & (arr <= band_hi)))
            if count > best_count:
                best_count = count
                best_level = pivot

        # Step 3 – require minimum touch count; fallback to simple extreme otherwise
        if best_level is None or best_count < min_touches:
            return float(arr.max() if kind == "high" else arr.min())

        return best_level

    @staticmethod
    def _find_next_level(series: pd.Series, current_level: float, direction: str) -> float | None:
        """Find the next S/R level beyond the current breakout level."""
        if direction == "up":
            candidates = series[series > current_level * 1.001]
            return float(candidates.min()) if len(candidates) else None
        else:
            candidates = series[series < current_level * 0.999]
            return float(candidates.max()) if len(candidates) else None
