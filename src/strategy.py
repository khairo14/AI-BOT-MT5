"""
strategy.py – AI/ML-based trading strategy.

Trains a RandomForestClassifier on recent OHLCV + indicator data and
produces BUY / SELL / HOLD signals with an associated confidence score.
"""

from __future__ import annotations

import os
import time
from typing import Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

from src.indicators import Indicators
from src.logger import get_logger

logger = get_logger("strategy")

SIGNAL_BUY = 1
SIGNAL_SELL = -1
SIGNAL_HOLD = 0


class TradingStrategy:
    """
    AI-driven trading strategy that:

    1. Computes technical indicators as features.
    2. Labels historical candles (future-close comparison).
    3. Trains a RandomForest pipeline.
    4. Generates trade signals with confidence scores.
    """

    def __init__(
        self,
        features: list[str],
        prediction_threshold: float = 0.60,
        train_lookback: int = 500,
        retrain_interval_hours: int = 24,
        model_dir: str = "models",
        model_file: str = "trading_model.joblib",
        indicator_params: Optional[dict] = None,
    ) -> None:
        self.features = features
        self.prediction_threshold = prediction_threshold
        self.train_lookback = train_lookback
        self.retrain_interval_seconds = retrain_interval_hours * 3600
        self.model_path = os.path.join(model_dir, model_file)
        self._model_dir = model_dir

        params = indicator_params or {}
        self.indicators = Indicators(**params)

        self._pipeline: Optional[Pipeline] = None
        self._last_trained: float = 0.0

    # ------------------------------------------------------------------
    # Feature / label engineering
    # ------------------------------------------------------------------

    def _build_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute indicators and return the feature matrix."""
        enriched = self.indicators.compute_all(df)
        available = [f for f in self.features if f in enriched.columns]
        missing = set(self.features) - set(available)
        if missing:
            logger.warning("Requested features not found and will be skipped: %s", missing)
        return enriched[available].copy()

    @staticmethod
    def _build_labels(df: pd.DataFrame, horizon: int = 1) -> pd.Series:
        """Label each bar: +1 (price up), -1 (price down), 0 (flat / noise)."""
        future_close = df["close"].shift(-horizon)
        pct_change = (future_close - df["close"]) / df["close"]
        threshold = pct_change.std() * 0.25
        labels = pd.Series(SIGNAL_HOLD, index=df.index, name="label")
        labels[pct_change > threshold] = SIGNAL_BUY
        labels[pct_change < -threshold] = SIGNAL_SELL
        return labels

    # ------------------------------------------------------------------
    # Training
    # ------------------------------------------------------------------

    def train(self, df: pd.DataFrame) -> None:
        """Train the RandomForest pipeline on *df*.

        Args:
            df: OHLCV DataFrame (must have at minimum 'open','high','low','close').
        """
        if len(df) < 50:
            logger.warning("Not enough data to train (%d rows). Skipping.", len(df))
            return

        X = self._build_features(df.iloc[-self.train_lookback :])
        y = self._build_labels(df.iloc[-self.train_lookback :])

        # Align and drop NaNs
        combined = pd.concat([X, y], axis=1).dropna()
        if len(combined) < 50:
            logger.warning("Too few clean samples after NaN removal (%d). Skipping.", len(combined))
            return

        X_clean = combined[X.columns]
        y_clean = combined["label"]

        X_train, X_val, y_train, y_val = train_test_split(
            X_clean, y_clean, test_size=0.2, shuffle=False
        )

        self._pipeline = Pipeline(
            [
                ("scaler", StandardScaler()),
                (
                    "clf",
                    RandomForestClassifier(
                        n_estimators=100,
                        max_depth=6,
                        class_weight="balanced",
                        random_state=42,
                        n_jobs=-1,
                    ),
                ),
            ]
        )
        self._pipeline.fit(X_train, y_train)
        self._last_trained = time.time()

        # Validation report
        y_pred = self._pipeline.predict(X_val)
        report = classification_report(y_val, y_pred, target_names=["SELL", "HOLD", "BUY"],
                                        labels=[-1, 0, 1], zero_division=0)
        logger.info("Model trained on %d samples.\nValidation report:\n%s", len(X_train), report)

        self._save_model()

    def _save_model(self) -> None:
        os.makedirs(self._model_dir, exist_ok=True)
        joblib.dump(self._pipeline, self.model_path)
        logger.info("Model saved to %s", self.model_path)

    def load_model(self) -> bool:
        """Load a previously saved model from disk.

        Returns:
            True if loaded successfully, False otherwise.
        """
        if not os.path.exists(self.model_path):
            logger.info("No saved model found at %s.", self.model_path)
            return False
        try:
            self._pipeline = joblib.load(self.model_path)
            logger.info("Model loaded from %s", self.model_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load model: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def needs_retraining(self) -> bool:
        """Return True if the model has never been trained or is stale."""
        return (
            self._pipeline is None
            or (time.time() - self._last_trained) > self.retrain_interval_seconds
        )

    def predict(self, df: pd.DataFrame) -> tuple[int, float]:
        """Generate a trading signal for the *latest* bar in *df*.

        Returns:
            A tuple of (signal, confidence) where signal ∈ {-1, 0, 1} and
            confidence ∈ [0, 1].  Returns (HOLD, 0.0) when the model is
            unavailable or data is insufficient.
        """
        if self._pipeline is None:
            logger.warning("Model not trained yet. Returning HOLD.")
            return SIGNAL_HOLD, 0.0

        X = self._build_features(df)
        latest = X.iloc[[-1]].dropna(axis=1)

        # Ensure all trained features are present
        trained_features = self._pipeline.named_steps["scaler"].feature_names_in_
        missing = set(trained_features) - set(latest.columns)
        if missing:
            logger.warning("Features missing in current bar: %s. Returning HOLD.", missing)
            return SIGNAL_HOLD, 0.0

        latest = latest[trained_features]

        proba = self._pipeline.predict_proba(latest)[0]
        classes = self._pipeline.classes_

        best_idx = int(np.argmax(proba))
        confidence = float(proba[best_idx])
        signal = int(classes[best_idx])

        if confidence < self.prediction_threshold:
            logger.debug(
                "Low confidence (%.2f < %.2f). Returning HOLD.", confidence, self.prediction_threshold
            )
            return SIGNAL_HOLD, confidence

        label = {SIGNAL_BUY: "BUY", SIGNAL_SELL: "SELL", SIGNAL_HOLD: "HOLD"}.get(signal, "HOLD")
        logger.info("Signal: %s | Confidence: %.2f", label, confidence)
        return signal, confidence
