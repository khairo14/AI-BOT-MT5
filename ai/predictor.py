"""
Price Predictor — LSTM model per symbol.

Trains on OHLCV data (H1 by default) to predict whether the next bar's
close will be higher than the current bar's close (probability 0–1).

Falls back gracefully to 0.5 if:
  - PyTorch is not installed (requirements-ml.txt not run)
  - No model has been trained yet for a symbol

Model files: ai/models/{symbol}_lstm.pt
Scaler files: ai/models/{symbol}_scaler.pkl
"""

from __future__ import annotations

import pickle
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

MODELS_DIR   = Path(__file__).parent / "models"
MODELS_DIR.mkdir(exist_ok=True)

SEQUENCE_LEN = 60   # look-back window in bars
HIDDEN_SIZE  = 64
NUM_LAYERS   = 2
EPOCHS       = 20
BATCH_SIZE   = 32


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _build_lstm():
    """Build LSTM model (only call when torch is available)."""
    import torch.nn as nn

    class _Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=5,
                hidden_size=HIDDEN_SIZE,
                num_layers=NUM_LAYERS,
                batch_first=True,
                dropout=0.2,
            )
            self.fc  = nn.Linear(HIDDEN_SIZE, 1)
            self.sig = nn.Sigmoid()

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.sig(self.fc(out[:, -1, :]))

    return _Net()


class PricePredictor:
    """Per-symbol LSTM price direction predictor (application singleton)."""

    def __init__(self):
        self._models:   dict[str, object] = {}   # symbol → nn.Module
        self._scalers:  dict[str, object] = {}   # symbol → StandardScaler
        self._metadata: dict[str, dict]   = {}   # symbol → {trained_at, accuracy, bars_used}
        self._training: set[str]          = set()
        self._lock = threading.Lock()
        self._load_all()

    # ── public API ────────────────────────────────────────────────────────────

    def predict(self, symbol: str, df: pd.DataFrame) -> float:
        """
        Return P(next bar close > current bar close) in [0, 1].
        Returns 0.5 (neutral) if no model is trained or torch unavailable.
        """
        if not _torch_available() or symbol not in self._models:
            return 0.5

        import torch

        features = _make_features(df)
        if features is None or len(features) < SEQUENCE_LEN:
            return 0.5

        scaler = self._scalers[symbol]
        scaled = scaler.transform(features[-SEQUENCE_LEN:])
        x      = torch.tensor(scaled, dtype=torch.float32).unsqueeze(0)

        model = self._models[symbol]
        model.eval()
        with torch.no_grad():
            prob = model(x).item()
        return float(prob)

    def train_async(self, symbol: str, df: pd.DataFrame) -> bool:
        """
        Start background training for a symbol. Returns False if already in progress.
        Poll status() to check completion.
        """
        with self._lock:
            if symbol in self._training:
                return False
            self._training.add(symbol)
        t = threading.Thread(target=self._train, args=(symbol, df), daemon=True)
        t.start()
        logger.info(f"LSTM training started for {symbol} ({len(df)} bars)")
        return True

    def is_training(self, symbol: str) -> bool:
        return symbol in self._training

    def is_trained(self, symbol: str) -> bool:
        return symbol in self._models

    def status(self) -> dict:
        """Return training status dict for all known symbols."""
        all_symbols = set(self._models) | set(self._training)
        return {
            sym: {
                "trained":  sym in self._models,
                "training": sym in self._training,
                **self._metadata.get(sym, {}),
            }
            for sym in all_symbols
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _train(self, symbol: str, df: pd.DataFrame) -> None:
        try:
            if not _torch_available():
                logger.warning("PyTorch not installed — run: pip install -r requirements-ml.txt")
                return
            self._do_train(symbol, df)
        except Exception as exc:
            logger.exception(f"LSTM training failed for {symbol}: {exc}")
        finally:
            with self._lock:
                self._training.discard(symbol)

    def _do_train(self, symbol: str, df: pd.DataFrame) -> None:
        import torch
        import torch.nn as nn
        from sklearn.preprocessing import StandardScaler

        features = _make_features(df)
        if features is None or len(features) < SEQUENCE_LEN + 10:
            logger.warning(f"Insufficient data for {symbol} training ({len(df)} bars)")
            return

        # Scale
        scaler = StandardScaler()
        scaled = scaler.fit_transform(features)

        # Build sequences: X[i] = seq of SEQUENCE_LEN bars, y[i] = 1 if next close > current
        X, y = [], []
        for i in range(len(scaled) - SEQUENCE_LEN):
            X.append(scaled[i: i + SEQUENCE_LEN])
            y.append(
                1.0 if features[i + SEQUENCE_LEN, 0] > features[i + SEQUENCE_LEN - 1, 0]
                else 0.0
            )

        X = torch.tensor(np.array(X), dtype=torch.float32)
        y = torch.tensor(np.array(y), dtype=torch.float32).unsqueeze(1)

        # Train / val split (80/20)
        split   = int(len(X) * 0.8)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        model     = _build_lstm()
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        criterion = nn.BCELoss()

        model.train()
        for _ in range(EPOCHS):
            for i in range(0, len(X_train), BATCH_SIZE):
                xb = X_train[i: i + BATCH_SIZE]
                yb = y_train[i: i + BATCH_SIZE]
                optimizer.zero_grad()
                criterion(model(xb), yb).backward()
                optimizer.step()

        # Validation accuracy
        model.eval()
        with torch.no_grad():
            preds    = (model(X_val) > 0.5).float()
            accuracy = (preds == y_val).float().mean().item()

        # Persist
        torch.save(model.state_dict(), MODELS_DIR / f"{symbol}_lstm.pt")
        with open(MODELS_DIR / f"{symbol}_scaler.pkl", "wb") as f:
            pickle.dump(scaler, f)

        with self._lock:
            self._models[symbol]   = model
            self._scalers[symbol]  = scaler
            self._metadata[symbol] = {
                "trained_at": datetime.utcnow().isoformat(),
                "accuracy":   round(accuracy, 4),
                "bars_used":  len(df),
            }

        logger.info(f"LSTM trained for {symbol} — val accuracy: {accuracy:.2%}")

    def _load_all(self) -> None:
        """Load any previously saved models from disk at startup."""
        if not _torch_available():
            return
        import torch

        for model_file in MODELS_DIR.glob("*_lstm.pt"):
            symbol       = model_file.stem.replace("_lstm", "")
            scaler_file  = MODELS_DIR / f"{symbol}_scaler.pkl"
            if not scaler_file.exists():
                continue
            try:
                model = _build_lstm()
                model.load_state_dict(torch.load(model_file, map_location="cpu", weights_only=True))
                model.eval()
                with open(scaler_file, "rb") as f:
                    scaler = pickle.load(f)
                self._models[symbol]  = model
                self._scalers[symbol] = scaler
                logger.info(f"Loaded LSTM model: {symbol}")
            except Exception as exc:
                logger.warning(f"Could not load model for {symbol}: {exc}")


def _make_features(df: pd.DataFrame) -> Optional[np.ndarray]:
    """
    Return (N, 5) feature array:
      col 0 — close return (close[i] - close[i-1]) / close[i-1]
      col 1 — bar range (high - low) / close
      col 2 — open-close body (close - open) / open
      col 3 — volume normalised by mean
      col 4 — upper wick (high - close) / close
    """
    needed = {"open", "high", "low", "close", "tick_volume"}
    if not needed.issubset(df.columns):
        return None

    close = df["close"].values.astype(float)
    open_ = df["open"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    vol   = df["tick_volume"].values.astype(float)

    eps     = 1e-10
    ret_c   = np.diff(close, prepend=close[0]) / (close + eps)
    ret_hl  = (high - low) / (close + eps)
    ret_oc  = (close - open_) / (open_ + eps)
    vol_n   = vol / (vol.mean() + eps)
    wick    = (high - close) / (close + eps)

    return np.column_stack([ret_c, ret_hl, ret_oc, vol_n, wick])


# Application-level singleton
predictor = PricePredictor()
