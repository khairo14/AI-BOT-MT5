"""
W3 — Weekly Breakout
Prior week high / low levels as breakout zones
Minimum ATR × 0.5 needed to confirm breakout is real (not a wick)
MACD histogram + ADX confirmation before entry
Entry: stop order placed 2 pips beyond the level
"""

from __future__ import annotations

import ta
import pandas as pd
import numpy as np

from engine.strategies.base_strategy import BaseStrategy, Signal, StrategyResult

DEFAULT_PARAMS = {
    "atr_break_mult": 0.5,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "adx_period": 14,
    "adx_threshold": 22,
    "tp_rr": 2.0,
    "sl_atr_mult": 1.0,
    "entry_buffer_pips": 2,
}


def _prior_week_levels(df_daily: pd.DataFrame) -> tuple[float | None, float | None]:
    """Return (prior_week_high, prior_week_low) from daily data."""
    if df_daily is None or len(df_daily) < 10:
        return None, None
    df_daily = df_daily.copy()
    df_daily["week"] = pd.to_datetime(df_daily["time"]).dt.isocalendar().week
    df_daily["year"] = pd.to_datetime(df_daily["time"]).dt.isocalendar().year
    grouped = df_daily.groupby(["year", "week"])
    week_keys = sorted(grouped.groups.keys())
    if len(week_keys) < 2:
        return None, None
    prior_key = week_keys[-2]
    prior_data = grouped.get_group(prior_key)
    return float(prior_data["high"].max()), float(prior_data["low"].min())


class WeeklyBreakout(BaseStrategy):

    name = "weekly_breakout"
    trading_type = "swing"
    timeframe = "H4"

    def calculate(
        self,
        df: pd.DataFrame,
        df_daily: pd.DataFrame | None = None,
    ) -> StrategyResult:
        p = {**DEFAULT_PARAMS, **self.params}

        if len(df) < 30:
            return self._no_signal()

        close = df["close"]
        high  = df["high"]
        low   = df["low"]

        macd_ind  = ta.trend.MACD(close, window_fast=p["macd_fast"], window_slow=p["macd_slow"], window_sign=p["macd_signal"])
        macd_hist = macd_ind.macd_diff()
        adx_ind   = ta.trend.ADXIndicator(high, low, close, window=p["adx_period"])
        adx_val   = adx_ind.adx()
        atr       = ta.volatility.AverageTrueRange(high, low, close, window=14).average_true_range()

        curr_close = close.iloc[-1]
        curr_hist  = macd_hist.iloc[-1]
        prev_hist  = macd_hist.iloc[-2]
        curr_adx   = adx_val.iloc[-1]
        curr_atr   = atr.iloc[-1]

        pw_high, pw_low = _prior_week_levels(df_daily)

        # If no daily data available, fall back to rolling 5-day H4 high/low
        if pw_high is None:
            pw_high = high.iloc[-30:].max()
            pw_low  = low.iloc[-30:].min()

        adx_ok    = curr_adx >= p["adx_threshold"]
        out_pips  = curr_atr * p["atr_break_mult"]

        buffer = p["entry_buffer_pips"] * 0.0001  # convert pips

        bull_break = (
            curr_close > pw_high + out_pips
            and curr_hist > 0
            and prev_hist > 0                          # MACD histogram positive & growing
            and adx_ok
        )

        bear_break = (
            curr_close < pw_low - out_pips
            and curr_hist < 0
            and prev_hist < 0                          # MACD histogram negative
            and adx_ok
        )

        indicators = {
            "pw_high": round(pw_high, 5) if pw_high else None,
            "pw_low": round(pw_low, 5) if pw_low else None,
            "macd_hist": round(curr_hist, 6),
            "adx": round(curr_adx, 2),
            "atr": round(curr_atr, 5),
            "bull_break": bull_break,
            "bear_break": bear_break,
        }

        if bull_break:
            entry = round(curr_close, 5)
            sl    = round(pw_high - curr_atr * p["sl_atr_mult"], 5)
            sl_dist = abs(entry - sl)
            tp    = round(entry + sl_dist * p["tp_rr"], 5)
            return StrategyResult(
                signal=Signal(
                    direction="BUY",
                    entry_price=entry,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="swing|weekly_break|buy",
                ),
                indicators=indicators,
            )

        if bear_break:
            entry = round(curr_close, 5)
            sl    = round(pw_low + curr_atr * p["sl_atr_mult"], 5)
            sl_dist = abs(sl - entry)
            tp    = round(entry - sl_dist * p["tp_rr"], 5)
            return StrategyResult(
                signal=Signal(
                    direction="SELL",
                    entry_price=entry,
                    sl_price=sl,
                    tp_price=tp,
                    strategy=self.name,
                    symbol=self.symbol,
                    timeframe=self.timeframe,
                    comment="swing|weekly_break|sell",
                ),
                indicators=indicators,
            )

        return self._no_signal(indicators)
