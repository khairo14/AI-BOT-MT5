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

import json
import threading
import time as _time
from pathlib import Path

import numpy as np
import pandas as pd
from loguru import logger

from ai.predictor import PricePredictor, predictor as _predictor_singleton

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "app.json"

# TTL cache — re-read app.json at most once every 5 s
# Protected by a lock because strategy runner calls this from multiple threads.
_cfg_cache: dict = {}
_cfg_loaded_at: float = 0.0
_CFG_TTL = 5.0
_cfg_lock = threading.Lock()

def _get_app_cfg() -> dict:
    global _cfg_cache, _cfg_loaded_at
    now = _time.monotonic()
    if now - _cfg_loaded_at < _CFG_TTL:
        return _cfg_cache   # fast path — no lock needed (stale read is acceptable)
    with _cfg_lock:
        # Re-check after acquiring lock to avoid double reload
        if now - _cfg_loaded_at < _CFG_TTL:
            return _cfg_cache
        try:
            _cfg_cache = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
        _cfg_loaded_at = now
    return _cfg_cache


class SignalScorer:

    # Default weights (sum = 1.0)
    _DEFAULT_W_LSTM   = 0.40
    _DEFAULT_W_RR     = 0.25
    _DEFAULT_W_TREND  = 0.20
    _DEFAULT_W_VOLUME = 0.15

    def __init__(self, pred: PricePredictor):
        self.predictor = pred

    def _weights(self, regime: str | None = None, trading_type: str = "day_trading") -> tuple[float, float, float, float]:
        """Return (W_LSTM, W_RR, W_TREND, W_VOLUME) for the given regime and trading_type.

        Resolution order:
          1. app.json ai.regime_weights[regime]        — adaptive per-regime profile
          2. app.json ai.scalping_scorer_weights        — scalping-specific base weights
          3. app.json ai.scorer_weights                 — static base (day_trading / swing)
          4. Class defaults
        """
        try:
            cfg = _get_app_cfg()
            ai  = cfg.get("ai", {})

            # Try regime-specific weights first
            if regime:
                rw = ai.get("regime_weights", {}).get(regime, {})
                if rw:
                    wl = float(rw.get("lstm",   self._DEFAULT_W_LSTM))
                    wr = float(rw.get("rr",     self._DEFAULT_W_RR))
                    wt = float(rw.get("trend",  self._DEFAULT_W_TREND))
                    wv = float(rw.get("volume", self._DEFAULT_W_VOLUME))
                    total = wl + wr + wt + wv
                    if total > 0:
                        return wl / total, wr / total, wt / total, wv / total

            # Scalping-specific weights take priority over generic scorer_weights
            if trading_type == "scalping":
                sw = ai.get("scalping_scorer_weights", {})
                if sw:
                    wl = float(sw.get("lstm",   0.15))
                    wr = float(sw.get("rr",     0.25))
                    wt = float(sw.get("trend",  0.40))
                    wv = float(sw.get("volume", 0.20))
                    total = wl + wr + wt + wv
                    if total > 0:
                        return wl / total, wr / total, wt / total, wv / total

            # Fall back to static balanced weights
            w = ai.get("scorer_weights", {})
            if w:
                wl = float(w.get("lstm",   self._DEFAULT_W_LSTM))
                wr = float(w.get("rr",     self._DEFAULT_W_RR))
                wt = float(w.get("trend",  self._DEFAULT_W_TREND))
                wv = float(w.get("volume", self._DEFAULT_W_VOLUME))
                total = wl + wr + wt + wv
                if total > 0:
                    return wl / total, wr / total, wt / total, wv / total
        except Exception as exc:
            logger.warning(f"SignalScorer._weights: config read failed ({exc}) — using class defaults")
        return (
            self._DEFAULT_W_LSTM, self._DEFAULT_W_RR,
            self._DEFAULT_W_TREND, self._DEFAULT_W_VOLUME
        )

    def score(
        self,
        symbol:       str,
        direction:    str,        # "BUY" or "SELL"
        entry:        float,
        sl:           float,
        tp:           float,
        df:           pd.DataFrame,   # primary timeframe OHLCV, most-recent bar last
        trading_type: str = "day_trading",
        regime:       str | None = None,  # market regime label from RegimeClassifier
    ) -> float:
        """
        Return a 0–1 confidence score for a pending signal.
        When regime is supplied, weights shift to the regime-specific profile
        from app.json ai.regime_weights (trending/ranging/volatile/quiet).
        Gracefully returns 0.5 on any error.
        """
        try:
            lstm   = self._lstm_score(symbol, direction, df, trading_type)
            rr     = self._rr_score(entry, sl, tp)
            trend  = self._trend_score(direction, df)
            volume = self._volume_score(df)

            W_LSTM, W_RR, W_TREND, W_VOLUME = self._weights(regime, trading_type)
            score = (
                W_LSTM   * lstm   +
                W_RR     * rr     +
                W_TREND  * trend  +
                W_VOLUME * volume
            )
            return round(float(np.clip(score, 0.0, 1.0)), 4)

        except Exception as exc:
            logger.warning(f"SignalScorer error [{symbol}]: {exc}")
            return 0.5

    def is_tradeable(self, confidence: float, trading_type: str = "day_trading") -> bool:
        """
        Ask the RL agent whether this signal's confidence clears the
        dynamically-learned threshold for the given trading type.
        Defaults to True (pass-through) if rl_agent_enabled=false or rl_manager unavailable.
        """
        try:
            cfg = _get_app_cfg()
            if not cfg.get("ai", {}).get("rl_agent_enabled", True):
                return True  # RL gate disabled — let all signals through
        except Exception:
            pass
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
        Returns 0.5 (neutral) when price_prediction_enabled=false.
        """
        try:
            cfg = _get_app_cfg()
            if not cfg.get("ai", {}).get("price_prediction_enabled", True):
                return 0.5  # LSTM disabled — contribute neutral score
        except Exception:
            pass
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
        Graduated EMA50 vs EMA200 trend-alignment score.

        Rather than a binary 1.0 / 0.1 flip, the score is proportional to how
        far apart the EMAs are (as % of EMA200).  This penalises weak or choppy
        trends gradually instead of treating a 1-pip separation identically to a
        100-pip separation.

        Scoring formula:
          gap_pct        = (EMA50 - EMA200) / |EMA200|   (positive = bullish)
          trend_strength = clip(gap_pct / 0.02, -1, 1)   (normalised to ±1 at ±2% gap)
          raw_score      = 0.5 + 0.4 × trend_strength     (range [0.1, 0.9])

        BUY  direction: raw_score  (high = trend aligned)
        SELL direction: 1 - raw_score (inverted)
        """
        if len(df) < 200:
            return 0.5
        close  = df["close"].values.astype(float)
        ema50  = _ema(close, 50)
        ema200 = _ema(close, 200)
        eps    = max(abs(ema200[-1]), 1e-8)
        gap_pct        = (ema50[-1] - ema200[-1]) / eps
        # MATH-2: normalise over 5% gap (was 2%). A 2% gap now scores 0.6 and a 5%+
        # gap scores 1.0, preserving discrimination for strong vs moderate trends.
        trend_strength = float(np.clip(gap_pct / 0.05, -1.0, 1.0))
        raw_score      = round(0.5 + 0.4 * trend_strength, 4)   # [0.1, 0.9]
        if direction.upper() == "BUY":
            return raw_score
        else:
            return round(1.0 - raw_score, 4)

    def _volume_score(self, df: pd.DataFrame) -> float:
        """
        Compare last bar's volume to its 20-bar average.
          at average → 0.5 | 2× above average → 1.0 | below average → 0.0
        """
        vol_col = "volume" if "volume" in df.columns else "tick_volume"
        if vol_col not in df.columns or len(df) < 20:
            return 0.5
        vol = df[vol_col].values.astype(float)
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
