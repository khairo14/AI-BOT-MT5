"""
Price Predictor — LSTM model per symbol × trading type.

Trains on OHLCV data at the natural timeframe for each trading type:
  scalping    → M5   (fast price action)
  day_trading → H1   (intraday trend)
  swing       → H4   (multi-day structure)

Falls back gracefully to 0.5 if:
  - PyTorch is not installed (requirements-ml.txt not run)
  - No model has been trained yet for a symbol+type key

Model files:  ai/models/{symbol}_{trading_type}_lstm.pt
Scaler files: ai/models/{symbol}_{trading_type}_scaler.pkl
"""

from __future__ import annotations

import pickle
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

MODELS_DIR = Path(__file__).parent / "models"
MODELS_DIR.mkdir(exist_ok=True)

# Natural training timeframe per trading type
TRADING_TYPE_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

SEQUENCE_LEN = 60   # look-back window in bars
HIDDEN_SIZE  = 64
NUM_LAYERS   = 2
EPOCHS       = 20
BATCH_SIZE   = 32
INPUT_SIZE   = 7    # close_return, hl_range, oc_body, volume_norm, upper_wick, is_near_news, atr_norm


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
        return True
    except ImportError:
        return False


def _get_device():
    """Return CUDA device if available, otherwise CPU."""
    import torch
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _build_lstm():
    """Build LSTM model (only call when torch is available)."""
    import torch.nn as nn

    class _Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(
                input_size=INPUT_SIZE,
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
        self._models:   dict[str, Any] = {}   # key → nn.Module
        self._scalers:  dict[str, Any] = {}   # key → StandardScaler
        self._metadata: dict[str, dict]   = {}   # symbol → {trained_at, accuracy, bars_used}
        self._training: set[str]          = set()
        self._lock = threading.Lock()
        self._load_all()

    # ── public API ────────────────────────────────────────────────────────────

    def predict(self, symbol: str, df: pd.DataFrame, trading_type: str = "day_trading") -> float:
        """
        Return P(next bar close > current bar close) in [0, 1].
        Returns 0.5 (neutral) if no model is trained or torch unavailable.
        """
        key = _model_key(symbol, trading_type)
        if not _torch_available() or key not in self._models:
            return 0.5

        import torch

        features = _make_features(df, symbol=symbol, trading_type=trading_type)
        if features is None or len(features) < SEQUENCE_LEN:
            return 0.5

        _device = _get_device()
        scaler  = self._scalers[key]
        scaled  = scaler.transform(features[-SEQUENCE_LEN:])
        x       = torch.tensor(scaled, dtype=torch.float32).unsqueeze(0).to(_device)

        model = self._models[key].to(_device)
        model.eval()
        with torch.no_grad():
            prob = model(x).item()
        return float(prob)

    def train_async(self, symbol: str, df: pd.DataFrame, trading_type: str = "day_trading") -> bool:
        """
        Start background training for a symbol+type. Returns False if already in progress.
        Poll status() to check completion.
        """
        key = _model_key(symbol, trading_type)
        with self._lock:
            if key in self._training:
                return False
            self._training.add(key)
        t = threading.Thread(target=self._train, args=(symbol, trading_type, df), daemon=True)
        t.start()
        logger.info(f"LSTM training started for {key} ({len(df)} bars)")
        return True

    def is_training(self, symbol: str, trading_type: str = "day_trading") -> bool:
        return _model_key(symbol, trading_type) in self._training

    def is_trained(self, symbol: str, trading_type: str = "day_trading") -> bool:
        return _model_key(symbol, trading_type) in self._models

    def status(self) -> dict:
        """Return training status dict for all known symbol+type keys."""
        all_keys = set(self._models) | set(self._training)
        return {
            key: {
                "trained":  key in self._models,
                "training": key in self._training,
                **self._metadata.get(key, {}),
            }
            for key in all_keys
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _train(self, symbol: str, trading_type: str, df: pd.DataFrame) -> None:
        key = _model_key(symbol, trading_type)
        try:
            if not _torch_available():
                logger.warning("PyTorch not installed — run: pip install -r requirements-ml.txt")
                return
            self._do_train(symbol, trading_type, df)
        except Exception as exc:
            logger.exception(f"LSTM training failed for {key}: {exc}")
        finally:
            with self._lock:
                self._training.discard(key)

    def _do_train(self, symbol: str, trading_type: str, df: pd.DataFrame) -> None:
        import torch
        import torch.nn as nn
        from sklearn.preprocessing import StandardScaler

        _device = _get_device()
        key = _model_key(symbol, trading_type)
        tf  = TRADING_TYPE_TF.get(trading_type, "H1")

        features = _make_features(df, symbol=symbol, trading_type=trading_type)
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

        X = torch.tensor(np.array(X), dtype=torch.float32).to(_device)
        y = torch.tensor(np.array(y), dtype=torch.float32).unsqueeze(1).to(_device)

        # Train / val split (80/20)
        split   = int(len(X) * 0.8)
        X_train, X_val = X[:split], X[split:]
        y_train, y_val = y[:split], y[split:]

        model     = _build_lstm().to(_device)
        optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
        criterion = nn.BCELoss()

        model.train()
        for _ in range(EPOCHS):
            for i in range(0, len(X_train), BATCH_SIZE):
                xb = X_train[i: i + BATCH_SIZE]
                yb = y_train[i: i + BATCH_SIZE]
                optimizer.zero_grad()
                criterion(model(xb), yb).backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()

        # Validation accuracy
        model.eval()
        with torch.no_grad():
            preds    = (model(X_val) > 0.5).float()
            accuracy = (preds == y_val).float().mean().item()

        # Accuracy gate: refuse to swap in a coin-flip model.
        # Keeps the previous trained version (if any) rather than degrading it.
        _MIN_ACCURACY = 0.55
        if accuracy < _MIN_ACCURACY:
            logger.warning(
                f"LSTM {key}: val accuracy {accuracy:.2%} < {_MIN_ACCURACY:.0%} threshold — "
                "model NOT saved. Existing model (if any) retained. Will retry on next retrain trigger."
            )
            return

        # Persist
        torch.save(model.state_dict(), MODELS_DIR / f"{key}_lstm.pt")
        with open(MODELS_DIR / f"{key}_scaler.pkl", "wb") as f:
            pickle.dump(scaler, f)

        with self._lock:
            self._models[key]   = model
            self._scalers[key]  = scaler
            self._metadata[key] = {
                "trained_at":   datetime.now(timezone.utc).isoformat(),
                "accuracy":     round(accuracy, 4),
                "bars_used":    len(df),
                "trading_type": trading_type,
                "timeframe":    tf,
            }

        # Persist metadata so it survives restarts
        import json as _json
        meta_file = MODELS_DIR / f"{key}_meta.json"
        try:
            meta_file.write_text(_json.dumps(self._metadata[key]), encoding="utf-8")
        except Exception:
            pass

        logger.info(f"LSTM trained: {key} ({tf}) — val accuracy: {accuracy:.2%}")

    def _load_all(self) -> None:
        """Load any previously saved models from disk at startup."""
        if not _torch_available():
            return
        import torch
        import json as _json

        for model_file in MODELS_DIR.glob("*_lstm.pt"):
            key         = model_file.stem.replace("_lstm", "")
            scaler_file = MODELS_DIR / f"{key}_scaler.pkl"
            if not scaler_file.exists():
                continue
            try:
                _load_device = _get_device()
                model = _build_lstm()
                model.load_state_dict(torch.load(model_file, map_location=_load_device, weights_only=True))
                model.to(_load_device)
                model.eval()
                with open(scaler_file, "rb") as f:
                    scaler = pickle.load(f)
                self._models[key]  = model
                self._scalers[key] = scaler
                # Restore metadata sidecar if present
                meta_file = MODELS_DIR / f"{key}_meta.json"
                if meta_file.exists():
                    try:
                        self._metadata[key] = _json.loads(meta_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                logger.info(f"Loaded LSTM model: {key}")
            except Exception as exc:
                logger.warning(f"Could not load model {key}: {exc}")


def _make_features(df: pd.DataFrame, symbol: str = "", trading_type: str = "day_trading") -> Optional[np.ndarray]:
    """
    Return (N, 7) feature array:
      col 0 — close return (close[i] - close[i-1]) / close[i-1]
      col 1 — bar range (high - low) / close
      col 2 — open-close body (close - open) / open
      col 3 — volume normalised by mean
      col 4 — upper wick (high - close) / close
      col 5 — is_near_news binary flag (1.0 within 30 min of high-impact event, else 0.0)
      col 6 — ATR(14) normalised by close (volatility regime indicator)
    """
    needed = {"open", "high", "low", "close"}
    vol_col = "volume" if "volume" in df.columns else "tick_volume"
    if not needed.issubset(df.columns) or vol_col not in df.columns:
        return None

    close = df["close"].values.astype(float)
    open_ = df["open"].values.astype(float)
    high  = df["high"].values.astype(float)
    low   = df["low"].values.astype(float)
    vol   = df[vol_col].values.astype(float)

    eps     = 1e-10
    ret_c   = np.diff(close, prepend=close[0]) / (close + eps)
    ret_hl  = (high - low) / (close + eps)
    ret_oc  = (close - open_) / (open_ + eps)
    vol_n   = vol / (vol.mean() + eps)
    wick    = (high - close) / (close + eps)

    # is_near_news: 1.0 if the symbol is currently in a news blackout window.
    # Applied only to the tail of the sequence matching the 45-min blackout window
    # rather than all bars uniformly — prevents train/inference distribution shift
    # (during training all 60 bars got the same current flag, which the model never
    # sees consistently at inference time for older bars in the sequence).
    try:
        from engine.news_filter import news_filter
        near_news, _ = news_filter.is_blocked(symbol, trading_type)
        news_flag = np.zeros(len(close))
        if near_news:
            tf_minutes = {"scalping": 5, "day_trading": 60, "swing": 240}.get(trading_type, 5)
            news_window_bars = max(1, 45 // tf_minutes)  # 45-min window (30 before + 15 after)
            news_flag[-news_window_bars:] = 1.0
    except Exception:
        news_flag = np.zeros(len(close))

    # ATR(14) normalised by close — volatility regime indicator (7th feature)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(
        np.abs(high - prev_close),
        np.abs(low  - prev_close),
    ))
    atr14 = pd.Series(tr).rolling(14, min_periods=1).mean().values
    atr_n = atr14 / (close + eps)

    return np.column_stack([ret_c, ret_hl, ret_oc, vol_n, wick, news_flag, atr_n])


def _model_key(symbol: str, trading_type: str) -> str:
    """Composite key: e.g. 'EURUSD_scalping'"""
    return f"{symbol}_{trading_type}"


# Application-level singleton
predictor = PricePredictor()
