"""
Signal Scorer — blends LSTM directional confidence with rule-based
technical quality metrics to produce a 0–1 confidence score per signal.

Scoring components (weighted blend):
  LSTM direction probability  — 40%
  Risk:Reward ratio quality   — 25%
  Trend alignment (EMA50/200) — 20%
  Volume confirmation         — 15%

A score of 0.75+ is considered high confidence (green badge in UI).
A score of 0.50–0.74 is medium (yellow).
Below 0.50 is low (gray).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger

from ai.predictor import PricePredictor, predictor as _predictor_singleton


class SignalScorer:

    W_LSTM   = 0.40
    W_RR     = 0.25
    W_TREND  = 0.20
    W_VOLUME = 0.15

    def __init__(self, pred: PricePredictor):
        self.predictor = pred

    def score(
        self,
        symbol:       str,
        direction:    str,        # "BUY" or "SELL"
        entry:        float,
        sl:           float,
        tp:           float,
        df:           pd.DataFrame,   # primary timeframe OHLCV, most-recent bar last
        trading_type: str = "day_trading",
    ) -> float:
        """
        Return a 0–1 confidence score for a pending signal.
        Gracefully returns 0.5 on any error.
        """
        try:
            lstm   = self._lstm_score(symbol, direction, df, trading_type)
            rr     = self._rr_score(entry, sl, tp)
            trend  = self._trend_score(direction, df)
            volume = self._volume_score(df)

            score = (
                self.W_LSTM   * lstm   +
                self.W_RR     * rr     +
                self.W_TREND  * trend  +
                self.W_VOLUME * volume
            )
            return round(float(np.clip(score, 0.0, 1.0)), 4)

        except Exception as exc:
            logger.warning(f"SignalScorer error [{symbol}]: {exc}")
            return 0.5

    def is_tradeable(self, confidence: float, trading_type: str = "day_trading") -> bool:
        """
        Ask the RL agent whether this signal's confidence clears the
        dynamically-learned threshold for the given trading type.
        Defaults to True (pass-through) if rl_manager is unavailable.
        """
        try:
            from ai.rl_agent import rl_manager
            return rl_manager.should_take_signal(trading_type, confidence)
        except Exception:
            return True

    # ── component scorers ────────────────────────────────────────────────────

    def _lstm_score(self, symbol: str, direction: str, df: pd.DataFrame, trading_type: str = "day_trading") -> float:
        """
        Align LSTM P(up) with signal direction.
        BUY: high P(up) → high score.
        SELL: low P(up) → high score.
        """
        prob = self.predictor.predict(symbol, df, trading_type)
        return prob if direction.upper() == "BUY" else (1.0 - prob)

    def _rr_score(self, entry: float, sl: float, tp: float) -> float:
        """
        Score based on Risk:Reward ratio, capped at 4:1 = 1.0.
          1:1 → 0.25 | 2:1 → 0.50 | 3:1 → 0.75 | ≥4:1 → 1.0
        """
        risk   = abs(entry - sl)
        reward = abs(tp - entry)
        if risk == 0:
            return 0.0
        return float(np.clip((reward / risk) / 4.0, 0.0, 1.0))

    def _trend_score(self, direction: str, df: pd.DataFrame) -> float:
        """
        EMA50 vs EMA200 trend alignment with signal direction.
        Aligned = 1.0, counter-trend = 0.1, not enough data = 0.5 (neutral).
        """
        if len(df) < 200:
            return 0.5
        close  = df["close"].values.astype(float)
        ema50  = _ema(close, 50)
        ema200 = _ema(close, 200)
        bullish = ema50[-1] > ema200[-1]
        if direction.upper() == "BUY":
            return 1.0 if bullish else 0.1
        else:
            return 1.0 if not bullish else 0.1

    def _volume_score(self, df: pd.DataFrame) -> float:
        """
        Compare last bar's volume to its 20-bar average.
          at average → 0.5 | 2× above average → 1.0 | below average → 0.0
        """
        if "tick_volume" not in df.columns or len(df) < 20:
            return 0.5
        vol = df["tick_volume"].values.astype(float)
        avg = vol[-20:].mean()
        if avg == 0:
            return 0.5
        return float(np.clip(vol[-1] / avg / 2.0, 0.0, 1.0))


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    alpha = 2.0 / (period + 1)
    ema   = np.empty_like(values)
    ema[0] = values[0]
    for i in range(1, len(values)):
        ema[i] = alpha * values[i] + (1 - alpha) * ema[i - 1]
    return ema


# Application-level singleton (wired to the predictor singleton)
scorer = SignalScorer(_predictor_singleton)
