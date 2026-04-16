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
    "rsi_min": 52,
    "rsi_max": 65,
    "max_spread_pips": 1.5,
    "sl_pips": 4,
    "tp1_rr": 1.2,          # partial close at 1.2R
    "rr": 2.0,              # tp2 (full close) at 2.0R
    "vol_confirm_mult": 1.2, # volume must exceed 20-bar avg × this (0 = disabled)
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

        # Volume confirmation on signal bar (M1 primary timeframe)
        vol_ok = True
        vol_mult = p.get("vol_confirm_mult", 1.2)
        if vol_mult > 0 and "volume" in df.columns and len(df) >= 21:
            curr_vol = df["volume"].iloc[-1]
            avg_vol  = df["volume"].iloc[-21:-1].mean()
            if avg_vol > 0:
                vol_ok = curr_vol > avg_vol * vol_mult

        indicators = {
            "ema_fast": round(curr_fast, 5),
            "ema_slow": round(curr_slow, 5),
            "rsi": round(curr_rsi, 2),
            "m5_bias": bias,
            "vol_ok": vol_ok,
        }

        pip = self._pip_size()

        # --- BUY: EMA fast crosses above slow, bias bullish, RSI in range ---
        if (
            prev_fast <= prev_slow
            and curr_fast > curr_slow
            and bias == "BULL"
            and p["rsi_min"] <= curr_rsi <= p["rsi_max"]
            and vol_ok
        ):
            sl  = round(curr_close - p["sl_pips"] * pip, 5)
            tp1 = round(curr_close + p["sl_pips"] * p["tp1_rr"] * pip, 5)
            tp2 = round(curr_close + p["sl_pips"] * p["rr"] * pip, 5)
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

        # --- SELL: EMA fast crosses below slow, bias bearish, RSI in range ---
        sell_rsi_min = 100 - p["rsi_max"]
        sell_rsi_max = 100 - p["rsi_min"]
        if (
            prev_fast >= prev_slow
            and curr_fast < curr_slow
            and bias == "BEAR"
            and sell_rsi_min <= curr_rsi <= sell_rsi_max
            and vol_ok
        ):
            sl  = round(curr_close + p["sl_pips"] * pip, 5)
            tp1 = round(curr_close - p["sl_pips"] * p["tp1_rr"] * pip, 5)
            tp2 = round(curr_close - p["sl_pips"] * p["rr"] * pip, 5)
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

    def _pip_size(self) -> float:
        """Return pip size based on symbol — accounts for metals, crypto and indices."""
        sym = self.symbol.upper()
        jpy_pairs = ("JPY", "HUF", "SEK", "NOK", "DKK")
        if any(sym.endswith(s) for s in jpy_pairs):
            return 0.01
        # Non-forex instruments have large nominal prices → use 0.1% of close price
        non_forex = ("BTC", "ETH", "XAU", "GOLD", "SILVER", "XAG",
                     "US30", "US100", "DE40", "UK100", "SPX", "NAS")
        if any(sym.startswith(p) or sym.endswith(p) for p in non_forex):
            # Not used for SL placement (strategies use ATR), but pip_size is kept
            # consistent at 1.0 so old callers get a safe non-zero value.
            return 1.0
        return 0.0001
