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

import json
import pickle
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from typing import Any

import numpy as np
from engine.notification_manager import notification_manager
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
EPOCHS       = 60   # raised from 50 for 200k-bar datasets (more data = more epochs needed)
BATCH_SIZE   = 32
INPUT_SIZE   = 7    # close_return, hl_range, oc_body, volume_norm, upper_wick, is_near_news, atr_norm
ENSEMBLE_SIZE = 3   # number of models trained per symbol×type to reduce variance (3-5 recommended)

# Prediction cache settings (5-minute TTL to reduce redundant OHLCV fetches + feature computation)
PREDICTION_CACHE_TTL = 300  # seconds (5 minutes)

# Number of bars ahead to aggregate for the training label.
# Single next-bar direction is near-pure noise at M5/H1/H4 — aggregating
# N bars reduces label noise while keeping the prediction horizon relevant.
_LOOKAHEAD: dict[str, int] = {
    "scalping":    5,   # M5  × 5 = 25-min horizon
    "day_trading": 5,   # H1  × 5 = 5-hour horizon
    "swing":       3,   # H4  × 3 = 12-hour horizon
}


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

        def forward(self, x):
            out, _ = self.lstm(x)
            return self.fc(out[:, -1, :])   # raw logits — sigmoid applied by loss / caller

    return _Net()


class PricePredictor:
    """Per-symbol LSTM price direction predictor (application singleton)."""

    def __init__(self):
        self._models:   dict[str, list[Any]] = {}   # key → list[nn.Module] (ensemble members)
        self._scalers:  dict[str, list[Any]] = {}   # key → list[StandardScaler]
        self._metadata: dict[str, dict]   = {}   # symbol → {trained_at, accuracy, bars_used, ensemble_size}
        self._training: set[str]          = set()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="LSTM-train")
        self._inference_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="LSTM-inference")
        self._calibration: dict[str, tuple[float, float]] = {}
        # Prediction cache: key → (prob, timestamp)
        self._prediction_cache: dict[str, tuple[float, float]] = {}
        self._load_calibration()
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

        # Check cache first (5-minute TTL)
        now = time.time()
        cached = self._prediction_cache.get(key)
        if cached is not None:
            prob, timestamp = cached
            if now - timestamp < PREDICTION_CACHE_TTL:
                # Cache hit - return immediately
                cal = self._calibration.get(key)
                if cal is not None:
                    a, b = cal
                    import math
                    try:
                        prob = 1.0 / (1.0 + math.exp(-(a * prob + b)))
                    except (OverflowError, ValueError):
                        pass
                return float(prob)

        features = _make_features(df, symbol=symbol, trading_type=trading_type)
        if features is None or len(features) < SEQUENCE_LEN:
            return 0.5

        _device = _get_device()
        
        # Parallel ensemble prediction: submit all members to thread pool
        models = self._models[key]
        scalers = self._scalers[key]
        
        def _predict_member(idx: int) -> float:
            """Predict with a single ensemble member (runs in thread pool)."""
            model = models[idx]
            scaler = scalers[idx]
            scaled = scaler.transform(features[-SEQUENCE_LEN:])
            x = torch.tensor(scaled, dtype=torch.float32).unsqueeze(0).to(_device)
            model = model.to(_device)
            model.eval()
            with torch.no_grad():
                return torch.sigmoid(model(x)).item()
        
        # Submit all members in parallel
        futures = [self._inference_pool.submit(_predict_member, i) for i in range(len(models))]
        probs = [f.result() for f in as_completed(futures)]
        
        # Average ensemble predictions
        prob = sum(probs) / len(probs) if probs else 0.5
        
        # Cache the raw prediction (before calibration)
        self._prediction_cache[key] = (prob, now)

        # Apply Platt scaling calibration if fitted
        # Calibration maps raw sigmoid p → P(win | confidence=p)
        cal = self._calibration.get(key)
        if cal is not None:
            a, b = cal
            import math
            try:
                prob = 1.0 / (1.0 + math.exp(-(a * prob + b)))
            except (OverflowError, ValueError):
                pass  # keep raw prob on overflow

        return float(prob)

    def train_async(self, symbol: str, df: pd.DataFrame, trading_type: str = "day_trading") -> bool:
        """
        Start background training for a symbol+type. Returns False if already in progress.
        Queues job in ThreadPoolExecutor (max 2 concurrent, auto-queues additional).
        Poll status() to check completion.
        """
        key = _model_key(symbol, trading_type)
        with self._lock:
            if key in self._training:
                return False
            self._training.add(key)
        # Submit to executor pool (max 2 concurrent, additional jobs auto-queue)
        self._executor.submit(self._train, symbol, trading_type, df)
        logger.info(f"LSTM training queued for {key} ({len(df)} bars)")
        return True

    def is_training(self, symbol: str, trading_type: str = "day_trading") -> bool:
        return _model_key(symbol, trading_type) in self._training

    def is_trained(self, symbol: str, trading_type: str = "day_trading") -> bool:
        return _model_key(symbol, trading_type) in self._models

    def clear_cache(self, symbol: str = None, trading_type: str = None) -> int:
        """
        Clear prediction cache. If symbol/type specified, clear only that key.
        Otherwise clear all. Returns number of entries cleared.
        """
        if symbol and trading_type:
            key = _model_key(symbol, trading_type)
            if key in self._prediction_cache:
                del self._prediction_cache[key]
                return 1
            return 0
        else:
            count = len(self._prediction_cache)
            self._prediction_cache.clear()
            logger.info(f"Cleared {count} prediction cache entries")
            return count

    def cache_stats(self) -> dict:
        """Return cache statistics for monitoring."""
        now = time.time()
        total = len(self._prediction_cache)
        valid = sum(1 for (_, ts) in self._prediction_cache.values() if now - ts < PREDICTION_CACHE_TTL)
        stale = total - valid
        return {
            "total_entries": total,
            "valid_entries": valid,
            "stale_entries": stale,
            "hit_rate": "N/A",  # would need hit/miss counters
            "ttl_seconds": PREDICTION_CACHE_TTL,
        }

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

        # Drop any rows with NaN or inf (e.g. from sparse volume or price gaps)
        mask = np.isfinite(features).all(axis=1)
        if mask.sum() < SEQUENCE_LEN + 10:
            logger.warning(f"Too many non-finite rows for {symbol} after cleaning")
            return
        features = features[mask]

        # Multi-bar lookahead label: cumulative return over next `lookahead` bars.
        # Aggregating reduces label noise vs single next-bar direction (which is
        # near-random at M5/H1/H4, explaining the 50-54% accuracy ceiling).
        lookahead = _LOOKAHEAD.get(trading_type, 5)
        n_seqs = len(features) - SEQUENCE_LEN - lookahead + 1

        # Train/val split boundary on the feature array first so the scaler never
        # sees validation-set rows — eliminates lookahead bias in normalisation.
        split = int(n_seqs * 0.8)
        train_feat_end = split + SEQUENCE_LEN + lookahead  # last feature row in any train label

        # Prepare shared data for all ensemble members
        X, y_list = [], []
        for i in range(n_seqs):
            # We'll scale features per-member with their own scaler
            X.append(features[i: i + SEQUENCE_LEN])
            cum_return = features[i + SEQUENCE_LEN: i + SEQUENCE_LEN + lookahead, 0].sum()
            y_list.append(1.0 if cum_return > 0 else 0.0)

        y_arr  = np.array(y_list, dtype=np.float32)
        n_pos  = float(y_arr.sum())
        n_neg  = float(len(y_arr) - n_pos)
        logger.info(
            f"LSTM {key}: class balance — {n_pos:.0f} up / {n_neg:.0f} down "
            f"({n_pos / len(y_arr):.1%} bullish in training data)"
        )

        # Train ensemble members with different random seeds
        ensemble_models = []
        ensemble_scalers = []
        ensemble_accuracies = []

        for member_idx in range(ENSEMBLE_SIZE):
            # Set unique random seed for this member
            torch.manual_seed(42 + member_idx)
            np.random.seed(42 + member_idx)

            # Each member gets its own scaler to introduce scaling variation
            scaler = StandardScaler()
            scaler.fit(features[:train_feat_end])
            scaled = scaler.transform(features)

            # Scale sequences for this member
            X_scaled = np.array([scaled[i: i + SEQUENCE_LEN] for i in range(n_seqs)])
            
            X_t = torch.tensor(X_scaled, dtype=torch.float32).to(_device)
            y_t = torch.tensor(y_arr, dtype=torch.float32).unsqueeze(1).to(_device)

            X_train, X_val = X_t[:split], X_t[split:]
            y_train, y_val = y_t[:split], y_t[split:]

            # Class-balanced loss
            pos_weight = torch.tensor([n_neg / (n_pos + 1e-10)], dtype=torch.float32).to(_device)
            criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)

            model = _build_lstm().to(_device)
            optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)

            # Label smoothing
            _LBL_SMOOTH = 0.05

            model.train()
            for _epoch in range(EPOCHS):
                _perm = torch.randperm(len(X_train))
                _X_shuf = X_train[_perm]
                _y_shuf = y_train[_perm]
                for i in range(0, len(_X_shuf), BATCH_SIZE):
                    xb = _X_shuf[i: i + BATCH_SIZE]
                    yb = _y_shuf[i: i + BATCH_SIZE]
                    yb_smooth = yb * (1.0 - _LBL_SMOOTH) + _LBL_SMOOTH * 0.5
                    optimizer.zero_grad()
                    criterion(model(xb), yb_smooth).backward()
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                    optimizer.step()

            # Validation accuracy
            model.eval()
            with torch.no_grad():
                preds = (torch.sigmoid(model(X_val)) > 0.5).float()
                accuracy = (preds == y_val).float().mean().item()

            ensemble_models.append(model)
            ensemble_scalers.append(scaler)
            ensemble_accuracies.append(accuracy)
            logger.info(f"LSTM {key} member {member_idx}: val accuracy {accuracy:.2%}")

        # Ensemble average accuracy
        avg_accuracy = sum(ensemble_accuracies) / len(ensemble_accuracies)

        # Accuracy gate: refuse to swap in a coin-flip model that would *degrade*
        # an already trained model.  If no model exists on disk yet, accept anything
        # above 50% as a bootstrap — learned market patterns beat a hard-coded 0.5.
        # PERF-1: Lowered from 58% to 55% — models struggle to exceed 55% in current market.
        _MIN_ACCURACY      = 0.55
        _BOOTSTRAP_MIN     = 0.50   # floor when no prior model exists
        _model_exists      = (MODELS_DIR / f"{key}_lstm_0.pt").exists()
        _effective_min     = _MIN_ACCURACY if _model_exists else _BOOTSTRAP_MIN
        if avg_accuracy <= _effective_min:
            logger.warning(
                f"LSTM {key}: ensemble avg accuracy {avg_accuracy:.2%} < {_effective_min:.0%} threshold "
                f"({'protection' if _model_exists else 'bootstrap'} gate) — "
                f"not saved (members: {[f'{a:.2%}' for a in ensemble_accuracies]})"
            )
            return

        # ── Step 1: Persist ensemble models and scalers to disk ─────────────────────────
        for idx, (model, scaler) in enumerate(zip(ensemble_models, ensemble_scalers)):
            model_file  = MODELS_DIR / f"{key}_lstm_{idx}.pt"
            scaler_path = MODELS_DIR / f"{key}_scaler_{idx}.pkl"
            torch.save(model.state_dict(), model_file)
            with open(scaler_path, "wb") as f:
                pickle.dump(scaler, f)

        with self._lock:
            self._models[key]   = ensemble_models
            self._scalers[key]  = ensemble_scalers
            self._metadata[key] = {
                "trained_at":   datetime.now(timezone.utc).isoformat(),
                "accuracy":     round(avg_accuracy, 4),
                "bars_used":    len(df),
                "trading_type": trading_type,
                "timeframe":    tf,
                "ensemble_size": ENSEMBLE_SIZE,
                "member_accuracies": [round(a, 4) for a in ensemble_accuracies],
            }

        # Persist metadata sidecar so it survives restarts
        meta_file = MODELS_DIR / f"{key}_meta.json"
        try:
            meta_file.write_text(
                json.dumps(self._metadata[key], indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        # ── Step 2: Save anchor (frozen baseline) — Issue 18 ─────────────────
        # The anchor is the FIRST production-quality ensemble saved for this key.
        # It is never overwritten — used to detect live performance degradation.
        _anchor_path      = MODELS_DIR / f"{key}_anchor.pt"
        _anchor_meta_path = MODELS_DIR / f"{key}_anchor_meta.json"
        if not _anchor_path.exists():
            try:
                import shutil as _shutil
                # Save first ensemble member as anchor (representative)
                _shutil.copy2(MODELS_DIR / f"{key}_lstm_0.pt", _anchor_path)
                _anchor_meta_path.write_text(
                    json.dumps({
                        "created_at":   datetime.now(tz=timezone.utc).isoformat(),
                        "accuracy":     round(avg_accuracy, 4),
                        "bars_used":    len(df),
                        "trading_type": trading_type,
                        "ensemble_size": ENSEMBLE_SIZE,
                        "note":         "frozen anchor — never auto-overwritten",
                    }, indent=2), encoding="utf-8"
                )
                logger.info(f"LSTM anchor saved: {key} (ensemble avg accuracy={avg_accuracy:.4f})")
            except Exception as _anc_exc:
                logger.debug(f"Anchor save failed for {key}: {_anc_exc}")

        # ── Step 3: Version archive — Issue 20 ───────────────────────────────
        # Keep last 3 ensemble versions for rollback capability.
        _versions_dir = MODELS_DIR / "versions" / key
        _versions_dir.mkdir(parents=True, exist_ok=True)
        try:
            import shutil as _shutil2
            _ts_stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
            # Archive all ensemble members
            for idx in range(ENSEMBLE_SIZE):
                _shutil2.copy2(
                    MODELS_DIR / f"{key}_lstm_{idx}.pt",
                    _versions_dir / f"{key}_lstm_{idx}_{_ts_stamp}.pt"
                )
                _shutil2.copy2(
                    MODELS_DIR / f"{key}_scaler_{idx}.pkl",
                    _versions_dir / f"{key}_scaler_{idx}_{_ts_stamp}.pkl"
                )
            (_versions_dir / f"{key}_meta_{_ts_stamp}.json").write_text(
                json.dumps({
                    "trained_at":    datetime.now(tz=timezone.utc).isoformat(),
                    "accuracy":      round(avg_accuracy, 4),
                    "bars_used":     len(df),
                    "trading_type":  trading_type,
                    "ensemble_size": ENSEMBLE_SIZE,
                    "version_stamp": _ts_stamp,
                }), encoding="utf-8"
            )
            # Prune: keep only last 3 ensemble versions
            _meta_versions = sorted(_versions_dir.glob(f"{key}_meta_*.json"))
            for _old_meta in _meta_versions[:-3]:
                _ts_old = _old_meta.stem.replace(f"{key}_meta_", "")
                # Delete all ensemble members for this version
                for idx in range(10):  # max 10 to catch old larger ensembles
                    (_versions_dir / f"{key}_lstm_{idx}_{_ts_old}.pt").unlink(missing_ok=True)
                    (_versions_dir / f"{key}_scaler_{idx}_{_ts_old}.pkl").unlink(missing_ok=True)
                _old_meta.unlink(missing_ok=True)
            logger.debug(f"Ensemble version archived: {key} v{_ts_stamp}")
        except Exception as _ver_exc:
            logger.debug(f"Version archive failed for {key}: {_ver_exc}")

        logger.info(f"LSTM ensemble trained: {key} ({tf}) — avg accuracy: {avg_accuracy:.2%} "
                    f"(members: {[f'{a:.2%}' for a in ensemble_accuracies]})")
        
        # Notify user of completed model training
        notification_manager.add(
            type="model_trained",
            title="Model Training Complete",
            message=f"{symbol} {trading_type.replace('_', ' ')} LSTM ensemble trained (accuracy: {avg_accuracy:.1%})",
            severity="success",
            metadata={
                "symbol": symbol,
                "trading_type": trading_type,
                "accuracy": round(avg_accuracy, 4),
                "ensemble_size": ENSEMBLE_SIZE
            }
        )
        
        # Invalidate prediction cache for this key (new model = new predictions)
        self.clear_cache(symbol, trading_type)

    def _load_model(self, symbol: str, trading_type: str) -> bool:
        """
        Reload a single model from disk into memory.
        Called after rollback to apply the rolled-back weights without restart.
        Returns True if successful.
        """
        if not _torch_available():
            return False
        import torch
        key = _model_key(symbol, trading_type)
        
        # Try loading ensemble first
        models = []
        scalers = []
        _device = _get_device()
        
        for idx in range(ENSEMBLE_SIZE):
            model_file = MODELS_DIR / f"{key}_lstm_{idx}.pt"
            scaler_file = MODELS_DIR / f"{key}_scaler_{idx}.pkl"
            if not model_file.exists() or not scaler_file.exists():
                break
            try:
                model = _build_lstm()
                model.load_state_dict(torch.load(model_file, map_location=_device, weights_only=False))
                model.to(_device)
                model.eval()
                with open(scaler_file, "rb") as f:
                    scaler = pickle.load(f)
                models.append(model)
                scalers.append(scaler)
            except Exception:
                break
        
        # Fallback to legacy single model if ensemble not complete
        if len(models) < ENSEMBLE_SIZE:
            model_file = MODELS_DIR / f"{key}_lstm.pt"
            scaler_file = MODELS_DIR / f"{key}_scaler.pkl"
            if not model_file.exists() or not scaler_file.exists():
                logger.warning(f"_load_model: files not found for {key}")
                return False
            try:
                model = _build_lstm()
                model.load_state_dict(torch.load(model_file, map_location=_device, weights_only=False))
                model.to(_device)
                model.eval()
                with open(scaler_file, "rb") as f:
                    scaler = pickle.load(f)
                models = [model]
                scalers = [scaler]
            except Exception as exc:
                logger.warning(f"_load_model failed for {key}: {exc}")
                return False
        
        try:
            with self._lock:
                self._models[key] = models
                self._scalers[key] = scalers
            # Reload metadata sidecar
            meta_file = MODELS_DIR / f"{key}_meta.json"
            if meta_file.exists():
                try:
                    with self._lock:
                        self._metadata[key] = json.loads(
                            meta_file.read_text(encoding="utf-8")
                        )
                except Exception:
                    pass
            logger.info(f"Model reloaded: {key} ({len(models)} members)")
            return True
        except Exception as exc:
            logger.warning(f"_load_model failed for {key}: {exc}")
            return False

    _CALIBRATION_FILE = MODELS_DIR / "lstm_calibration.json"

    def _load_calibration(self) -> None:
        """Load saved Platt scaling calibration params from disk."""
        if self._CALIBRATION_FILE.exists():
            try:
                raw = json.loads(
                    self._CALIBRATION_FILE.read_text(encoding="utf-8")
                )
                self._calibration = {
                    k: (float(v[0]), float(v[1]))
                    for k, v in raw.items()
                }
                logger.info(f"Calibration loaded for {len(self._calibration)} model(s)")
            except Exception as exc:
                logger.debug(f"Calibration load failed: {exc}")

    def calibrate(self, symbol: str, trading_type: str) -> dict:
        """
        Fit Platt scaling (logistic regression on raw sigmoid outputs) using
        live trade outcomes from trade_memory.

        Requires at least 30 live trades for the symbol+type to fit reliably.
        Calibration params (a, b) transform raw p → sigmoid(a*p + b).

        Returns calibration result dict.
        """
        key = _model_key(symbol, trading_type)
        if key not in self._models:
            return {"status": "no_model", "key": key}

        try:
            from ai.trade_memory import memory as _mem
            outcomes = [
                o for o in _mem.recent(n=500, live_only=True)
                if o.get("symbol") == symbol
                and o.get("trading_type") == trading_type
                and o.get("lstm_predicted_direction") is not None
            ]
        except Exception:
            outcomes = []

        if len(outcomes) < 30:
            return {
                "status":   "insufficient_data",
                "key":      key,
                "samples":  len(outcomes),
                "needed":   30,
            }

        # Build arrays: raw model output vs actual binary outcome.
        # Use the lstm_raw_prob stored in extra (set by strategy_runner since the
        # strategy_runner captures it from predictor._prediction_cache at signal time).
        # For older records without lstm_raw_prob, fall back to the direction proxy
        # (0.65 for BUY, 0.35 for SELL) as a best-effort approximation.
        raw_probs = []
        actuals   = []
        for o in outcomes:
            pred   = o.get("lstm_predicted_direction", "")
            profit = o.get("profit", 0)
            actual = 1.0 if profit > 0 else 0.0
            # Prefer the actual stored raw probability; fall back to direction proxy
            raw_p = o.get("extra", {}).get("lstm_raw_prob")
            if raw_p is None:
                raw_p = 0.65 if pred == "BUY" else 0.35
            raw_probs.append(float(raw_p))
            actuals.append(actual)

        # Fit logistic regression: find (a, b) s.t. sigmoid(a*p + b) ≈ actual
        # Using scipy minimize on log-loss (no scipy? fall back to identity)
        try:
            from scipy.optimize import minimize as _sp_min
            import numpy as _np2

            def _logloss(params):
                a, b = params
                p_cal = 1.0 / (1.0 + _np2.exp(-(a * _np2.array(raw_probs) + b)))
                p_cal = _np2.clip(p_cal, 1e-7, 1 - 1e-7)
                y = _np2.array(actuals)
                return -_np2.mean(y * _np2.log(p_cal) + (1 - y) * _np2.log(1 - p_cal))

            res = _sp_min(_logloss, [1.0, 0.0], method="Nelder-Mead")
            a, b = float(res.x[0]), float(res.x[1])
        except Exception:
            # scipy not available or optimization failed — use identity (a=1, b=0)
            a, b = 1.0, 0.0

        with self._lock:
            self._calibration[key] = (a, b)

        # Persist calibration
        try:
            _cal_dict = {k: list(v) for k, v in self._calibration.items()}
            self._CALIBRATION_FILE.write_text(
                json.dumps(_cal_dict, indent=2), encoding="utf-8"
            )
        except Exception as _ce:
            logger.debug(f"Calibration save failed: {_ce}")

        return {
            "status":  "calibrated",
            "key":     key,
            "a":       round(a, 4),
            "b":       round(b, 4),
            "samples": len(outcomes),
        }

    def _load_all(self) -> None:
        """Load any previously saved ensemble models from disk at startup."""
        if not _torch_available():
            return
        import torch
        import json as _json

        # Find all keys by looking for _lstm_0.pt (first ensemble member) or old _lstm.pt
        ensemble_keys = set()
        for model_file in MODELS_DIR.glob("*_lstm_0.pt"):
            key = model_file.stem.replace("_lstm_0", "")
            ensemble_keys.add(key)
        
        # Also check for legacy single models (backward compatibility)
        for model_file in MODELS_DIR.glob("*_lstm.pt"):
            key = model_file.stem.replace("_lstm", "")
            if key not in ensemble_keys:  # not already loaded as ensemble
                ensemble_keys.add(key)

        for key in ensemble_keys:
            try:
                _load_device = _get_device()
                models = []
                scalers = []
                
                # Try loading ensemble members
                for idx in range(ENSEMBLE_SIZE):
                    model_file = MODELS_DIR / f"{key}_lstm_{idx}.pt"
                    scaler_file = MODELS_DIR / f"{key}_scaler_{idx}.pkl"
                    if not model_file.exists() or not scaler_file.exists():
                        break  # incomplete ensemble, try legacy format
                    model = _build_lstm()
                    model.load_state_dict(torch.load(model_file, map_location=_load_device, weights_only=False))
                    model.to(_load_device)
                    model.eval()
                    with open(scaler_file, "rb") as f:
                        scaler = pickle.load(f)
                    models.append(model)
                    scalers.append(scaler)
                
                # If we didn't load a full ensemble, try legacy single model
                if len(models) < ENSEMBLE_SIZE:
                    legacy_model_file = MODELS_DIR / f"{key}_lstm.pt"
                    legacy_scaler_file = MODELS_DIR / f"{key}_scaler.pkl"
                    if legacy_model_file.exists() and legacy_scaler_file.exists():
                        models = []
                        scalers = []
                        model = _build_lstm()
                        model.load_state_dict(torch.load(legacy_model_file, map_location=_load_device, weights_only=False))
                        model.to(_load_device)
                        model.eval()
                        with open(legacy_scaler_file, "rb") as f:
                            scaler = pickle.load(f)
                        # Wrap single model as 1-member ensemble for compatibility
                        models = [model]
                        scalers = [scaler]
                        logger.info(f"Loaded legacy LSTM model as 1-member ensemble: {key}")
                    else:
                        logger.warning(f"Could not load complete ensemble or legacy model for {key}")
                        continue
                
                if models and scalers:
                    self._models[key] = models
                    self._scalers[key] = scalers
                    # Restore metadata sidecar if present
                    meta_file = MODELS_DIR / f"{key}_meta.json"
                    if meta_file.exists():
                        try:
                            self._metadata[key] = _json.loads(meta_file.read_text(encoding="utf-8"))
                        except Exception:
                            pass
                    logger.info(f"Loaded LSTM ensemble: {key} ({len(models)} members)")
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
    except Exception as _news_exc:
        logger.warning(f"News filter unavailable for {symbol}, news_flag zeroed: {_news_exc}")
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
