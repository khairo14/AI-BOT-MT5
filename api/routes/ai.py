"""
AI REST endpoints.

LSTM:
  GET  /ai/status                       — training status for all symbol+type keys
  POST /ai/train/{symbol}               — trigger LSTM training (background)
  GET  /ai/confidence/{symbol}          — LSTM directional probability

RL Agent:
  GET  /ai/rl/status                    — RL agent params per trading type
  POST /ai/rl/reset/{trading_type}      — reset RL agent to defaults

Trade Memory:
  GET  /ai/memory/stats                 — win rate, avg PnL, etc.
  GET  /ai/memory/recent                — last N trade outcomes
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ai.predictor import predictor, TRADING_TYPE_TF
from ai.rl_agent import rl_manager
from ai.trade_memory import memory

router = APIRouter()

TRADING_TYPE = Literal["scalping", "day_trading", "swing"]

# Default bar counts per trading type — match run_retrain.py and auto-retrain settings.
_TRAIN_BARS: dict[str, int] = {
    "scalping":    200_000,  # M5  ≈ 2 years
    "day_trading":  50_000,  # H1  ≈ 5.7 years
    "swing":         30_000,  # H4  ≈ 13.7 years
}


class TrainRequest(BaseModel):
    trading_type: TRADING_TYPE = "day_trading"
    bars: int = 0   # 0 = auto-select by trading_type (recommended)


@router.get("/status")
def ai_status():
    """Return LSTM training status for all symbols."""
    return predictor.status()


# ───────────────────────────────────
# Train-all (must be declared BEFORE /{symbol})
# ───────────────────────────────────

class TrainAllRequest(BaseModel):
    bars: int = 0   # 0 = auto-select by trading_type (recommended)


@router.post("/train/all")
async def train_all_symbols(req: TrainAllRequest = TrainAllRequest()):
    """
    Queues LSTM retraining for all enabled symbols × trading types and returns
    immediately.  Covers both symbols.json (static) and scanner.json (dynamic).
    A background task does fetching + dispatching in batches:

        for each mode  (scalping → day_trading → swing)
            for each pair  → fetch OHLCV + sleep 300 ms
            sleep 2 s      ← mode boundary rest
            for each pair  → dispatch train thread + sleep 100 ms
            sleep 2 s      ← mode boundary rest between trains
    """
    from api.main import get_mt5_client
    import json
    from datetime import datetime, timezone, timedelta

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    CONFIG_PATH = Path(__file__).parent.parent.parent / "config"
    try:
        symbols_cfg = json.loads((CONFIG_PATH / "symbols.json").read_text(encoding="utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=500, detail="Cannot read symbols.json")

    # Merge scanner.json symbols so train/all covers every symbol the bot may trade
    try:
        scanner_cfg = json.loads((CONFIG_PATH / "scanner.json").read_text(encoding="utf-8-sig"))
    except Exception:
        scanner_cfg = {}

    async def _bg_task() -> None:
        for trading_type in ("scalping", "day_trading", "swing"):
            sym_list = symbols_cfg.get(trading_type, [])
            if not isinstance(sym_list, list):
                continue
            tf_str = TRADING_TYPE_TF.get(trading_type, "H1")

            # Build deduplicated symbol list: symbols.json + scanner.json
            seen: set[str] = set()
            entries: list[str] = []
            for e in sym_list:
                sym = e.get("symbol") if isinstance(e, dict) else e
                enabled = e.get("enabled", False) if isinstance(e, dict) else True
                if sym and enabled and sym not in seen:
                    entries.append(sym)
                    seen.add(sym)
            for sym in scanner_cfg.get(trading_type, {}).get("symbols", []):
                if sym and sym not in seen:
                    entries.append(sym)
                    seen.add(sym)

            # ── Phase 1: fetch each symbol ────────────────────────────────────
            mode_bars = req.bars if req.bars > 0 else _TRAIN_BARS.get(trading_type, 5_000)
            ohlcv: dict[str, object] = {}
            for symbol in entries:
                if not symbol or predictor.is_training(symbol, trading_type):
                    continue
                # Scalping: use date-range fetch to get full 2-year M5 dataset
                if trading_type == "scalping":
                    _date_to = datetime.now(tz=timezone.utc)
                    _date_from = _date_to - timedelta(days=694)
                    df = await asyncio.to_thread(
                        client.get_ohlcv_range, symbol, tf_str, _date_from, _date_to
                    )
                else:
                    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, mode_bars)
                ohlcv[symbol] = df
                await asyncio.sleep(0.3)  # rest between pairs

            # ── Mode boundary rest ─────────────────────────────────────────────
            await asyncio.sleep(2.0)

            # ── Phase 2: dispatch training threads ────────────────────────────
            for symbol, df in ohlcv.items():
                if df is None or df.empty:
                    continue
                predictor.train_async(symbol, df, trading_type)
                await asyncio.sleep(0.1)  # rest between dispatches

            # ── Mode boundary rest ─────────────────────────────────────────────
            await asyncio.sleep(2.0)

    asyncio.create_task(_bg_task())
    return {"status": "queued", "detail": "Training running in background — poll /ai/status"}


@router.post("/train/{symbol}")
async def train_symbol(symbol: str, req: TrainRequest = TrainRequest()):
    """
    Trigger background LSTM training for a symbol.
    Uses the natural timeframe for the trading_type:
      scalping → M5, day_trading → H1, swing → H4
    Returns immediately — poll GET /ai/status to check completion.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    if predictor.is_training(symbol, req.trading_type):
        return {"status": "already_training", "symbol": symbol, "trading_type": req.trading_type}

    tf_str = TRADING_TYPE_TF.get(req.trading_type, "H1")
    bars   = req.bars if req.bars > 0 else _TRAIN_BARS.get(req.trading_type, 5_000)

    # Scalping: use date-range fetch to get the full 2-year M5 dataset.
    # get_ohlcv(bars) hits MT5's ~99k bar cap and only returns ~1 year.
    if req.trading_type == "scalping":
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        _date_to = _dt.now(tz=_tz.utc)
        _date_from = _date_to - _td(days=694)
        df = await asyncio.to_thread(client.get_ohlcv_range, symbol, tf_str, _date_from, _date_to)
    else:
        df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, bars)

    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {symbol} ({tf_str})")

    started = predictor.train_async(symbol, df, req.trading_type)
    return {
        "status":       "training_started" if started else "already_training",
        "symbol":       symbol,
        "trading_type": req.trading_type,
        "timeframe":    tf_str,
        "bars":         len(df),
    }


@router.get("/confidence/{symbol}")
async def get_confidence(symbol: str, trading_type: TRADING_TYPE = "day_trading"):
    """
    Get LSTM directional probability for a symbol+trading_type.
    p_up + p_down = 1.0. Returns 0.5/0.5 if no model is trained yet.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    tf_str = TRADING_TYPE_TF.get(trading_type, "H1")
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, 100)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No data for {symbol} ({tf_str})")

    p_up = predictor.predict(symbol, df, trading_type)
    key  = f"{symbol}_{trading_type}"
    return {
        "symbol":        symbol,
        "trading_type":  trading_type,
        "timeframe":     tf_str,
        "p_up":          round(p_up, 4),
        "p_down":        round(1.0 - p_up, 4),
        "model_trained": predictor.is_trained(symbol, trading_type),
        **predictor.status().get(key, {}),
    }

@router.post("/models/rollback/{symbol}/{trading_type}")
async def rollback_model(symbol: str, trading_type: TRADING_TYPE):
    """
    Roll back a symbol's LSTM ensemble to the previous version.
    Uses the model version archive in ai/models/versions/.
    Handles both ensemble (3 members) and legacy single models.
    """
    from pathlib import Path
    import shutil
    from ai.predictor import ENSEMBLE_SIZE
    
    key = f"{symbol}_{trading_type}"
    _versions_dir = Path("ai/models/versions") / key
    if not _versions_dir.exists():
        raise HTTPException(status_code=404, detail=f"No version archive for {key}")
    
    # Find version timestamps from metadata files
    _meta_versions = sorted(_versions_dir.glob(f"{key}_meta_*.json"))
    if len(_meta_versions) < 2:
        raise HTTPException(status_code=404, detail=f"Need at least 2 versions to rollback — only {len(_meta_versions)} found")
    
    # Second-to-last is the previous version
    _prev_meta = _meta_versions[-2]
    _ts_stamp = _prev_meta.stem.replace(f"{key}_meta_", "")
    _models_dir = Path("ai/models")
    
    try:
        # Check if this is an ensemble or legacy single model
        _ensemble_files = list(_versions_dir.glob(f"{key}_lstm_*_{_ts_stamp}.pt"))
        is_ensemble = len(_ensemble_files) >= ENSEMBLE_SIZE
        
        if is_ensemble:
            # Rollback ensemble members
            for idx in range(ENSEMBLE_SIZE):
                _prev_pt = _versions_dir / f"{key}_lstm_{idx}_{_ts_stamp}.pt"
                _prev_scaler = _versions_dir / f"{key}_scaler_{idx}_{_ts_stamp}.pkl"
                if _prev_pt.exists():
                    shutil.copy2(_prev_pt, _models_dir / f"{key}_lstm_{idx}.pt")
                if _prev_scaler.exists():
                    shutil.copy2(_prev_scaler, _models_dir / f"{key}_scaler_{idx}.pkl")
        else:
            # Legacy single model rollback
            _prev_pt = _versions_dir / f"{key}_lstm_{_ts_stamp}.pt"
            _prev_scaler = _versions_dir / f"{key}_scaler_{_ts_stamp}.pkl"
            if _prev_pt.exists():
                shutil.copy2(_prev_pt, _models_dir / f"{key}_lstm.pt")
            if _prev_scaler.exists():
                shutil.copy2(_prev_scaler, _models_dir / f"{key}_scaler.pkl")
        
        # Copy metadata
        if _prev_meta.exists():
            shutil.copy2(_prev_meta, _models_dir / f"{key}_meta.json")
        
        # Reload the predictor to pick up the rolled-back model
        reloaded = predictor._load_model(symbol, trading_type)
        return {
            "status":        "rolled_back" if reloaded else "rolled_back_reload_failed",
            "key":           key,
            "version_stamp": _ts_stamp,
            "model_type":    "ensemble" if is_ensemble else "legacy",
            "members":       ENSEMBLE_SIZE if is_ensemble else 1,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}")
    
@router.post("/models/calibrate/{symbol}/{trading_type}")
async def calibrate_model(symbol: str, trading_type: TRADING_TYPE):
    """
    Fit Platt scaling calibration for a symbol's LSTM model using live trade outcomes.
    Requires at least 30 live trades. Run periodically as live trade data accumulates.
    """
    result = predictor.calibrate(symbol, trading_type)
    if result.get("status") == "no_model":
        raise HTTPException(status_code=404, detail=f"No model for {symbol}/{trading_type}")
    return result

@router.post("/models/calibrate/all")
async def calibrate_all_models():
    """Calibrate all trained models using available live trade outcomes."""
    import json as _json
    from pathlib import Path as _Path
    results = {}
    _symbols_cfg_path = _Path("config/symbols.json")
    try:
        _syms_cfg = _json.loads(_symbols_cfg_path.read_text(encoding="utf-8"))
        for tt in ("scalping", "day_trading", "swing"):
            for entry in _syms_cfg.get(tt, []):
                sym = entry.get("symbol") if isinstance(entry, dict) else entry
                if sym and predictor.is_trained(sym, tt):
                    results[f"{sym}_{tt}"] = predictor.calibrate(sym, tt)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return results
# ───────────────────────────────────
# RL Agent endpoints
# ───────────────────────────────────
@router.get("/lstm/accuracy")
def get_lstm_accuracy(
    trading_type: Optional[str] = Query(None),
    min_samples: int = Query(20),
):
    """Return live LSTM prediction accuracy vs actual trade outcomes."""
    from ai.trade_memory import memory
    return memory.lstm_accuracy(
        trading_type=trading_type,
        min_samples=min_samples,
        live_only=True,
    )


@router.get("/lstm/calibration")
def get_lstm_calibration(
    trading_type: Optional[str] = Query(None),
    min_samples: int = Query(20),
    bins: int = Query(10, ge=5, le=20),
):
    """
    Return calibration curve data: predicted confidence vs actual win rate.
    
    Bins predictions into confidence ranges (e.g., 50-55%, 55-60%, ...) and
    computes actual win rate for trades in each bin. Ideal calibration shows
    predicted confidence matching actual outcomes (diagonal line).
    
    Returns:
    - calibration_curve: List of {bin_range, predicted_conf, actual_win_rate, sample_count}
    - overall_accuracy: Overall prediction accuracy
    - calibration_error: Mean absolute difference between predicted and actual
    - is_well_calibrated: True if calibration error < 0.10 (10%)
    """
    from ai.trade_memory import memory
    
    # Get trades with confidence predictions
    outcomes = memory.recent(
        n=memory.MAX_BUFFER,
        trading_type=trading_type,
        live_only=True,
    )
    
    # Filter for trades with confidence > 0 (some old trades may not have it)
    tracked = [o for o in outcomes if o.get("confidence", 0) > 0 and o.get("profit") is not None]
    
    if len(tracked) < min_samples:
        return {
            "tracked": len(tracked),
            "min_samples": min_samples,
            "sufficient_data": False,
            "message": f"Need at least {min_samples} trades with confidence data"
        }
    
    # Create bins (e.g., [0.40, 0.45, 0.50, ..., 0.90])
    bin_step = (0.90 - 0.40) / bins
    bin_edges = [0.40 + i * bin_step for i in range(bins + 1)]
    
    # Assign trades to bins
    calibration_data = []
    total_error = 0.0
    bins_with_data = 0
    
    for i in range(bins):
        bin_start = bin_edges[i]
        bin_end = bin_edges[i + 1]
        bin_center = (bin_start + bin_end) / 2
        
        # Find trades in this confidence bin
        bin_trades = [
            t for t in tracked
            if bin_start <= t.get("confidence", 0) < bin_end
        ]
        
        if len(bin_trades) == 0:
            continue  # Skip empty bins
        
        # Calculate actual win rate in this bin
        wins = sum(1 for t in bin_trades if t.get("profit", 0) > 0)
        actual_win_rate = wins / len(bin_trades)
        
        # Predicted confidence is the bin center
        predicted_conf = bin_center
        
        # Calibration error for this bin
        error = abs(predicted_conf - actual_win_rate)
        total_error += error
        bins_with_data += 1
        
        calibration_data.append({
            "bin_range": f"{bin_start:.0%}-{bin_end:.0%}",
            "bin_start": round(bin_start, 2),
            "bin_end": round(bin_end, 2),
            "predicted_conf": round(predicted_conf, 3),
            "actual_win_rate": round(actual_win_rate, 3),
            "sample_count": len(bin_trades),
            "calibration_error": round(error, 3),
        })
    
    # Overall calibration metrics
    mean_calibration_error = total_error / bins_with_data if bins_with_data > 0 else 0.0
    is_well_calibrated = mean_calibration_error < 0.10
    
    # Overall accuracy (from existing endpoint)
    accuracy_data = memory.lstm_accuracy(
        trading_type=trading_type,
        min_samples=min_samples,
        live_only=True,
    )
    
    return {
        "calibration_curve": calibration_data,
        "overall_accuracy": accuracy_data.get("overall_accuracy", 0.0),
        "mean_calibration_error": round(mean_calibration_error, 3),
        "is_well_calibrated": is_well_calibrated,
        "total_trades": len(tracked),
        "bins_with_data": bins_with_data,
        "trading_type": trading_type or "all",
    }


@router.get("/lstm/accuracy/history")
def get_accuracy_history(
    trading_type: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
):
    """
    Return time-series history of LSTM prediction accuracy.
    
    Tracks overall accuracy over time to detect model degradation and trigger auto-retraining.
    Snapshots are created periodically by calling trade_memory.snapshot_accuracy().
    
    Args:
        trading_type: Optional filter for scalping | day_trading | swing
        days: Number of days of history to return (1-365, default 30)
    
    Returns:
        - snapshots: List of {timestamp, overall_accuracy, correct, total, by_symbol}
        - current: Latest snapshot
        - statistics: min/max/avg accuracy over time period
        - degradation_alert: True if accuracy dropped >10% from peak
    """
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    import json
    
    _history_path = Path("ai/data") / "lstm_accuracy_history.jsonl"
    
    if not _history_path.exists():
        # No history yet — create first snapshot
        from ai.trade_memory import memory
        memory.snapshot_accuracy(trading_type=trading_type, min_samples=10)
        
        # Check again
        if not _history_path.exists():
            return {
                "snapshots": [],
                "message": "No accuracy history available yet (insufficient trades)"
            }
    
    # Read JSONL history file
    snapshots = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    with open(_history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            
            # Filter by trading_type if specified
            if trading_type and entry.get("trading_type") != trading_type and entry.get("trading_type") != "all":
                continue
            
            # Parse timestamp (strip trailing Z, already has timezone offset)
            ts = datetime.fromisoformat(entry["timestamp"].rstrip("Z"))
            if ts < cutoff:
                continue  # Skip entries older than requested days
            
            snapshots.append({
                "timestamp": entry["timestamp"],
                "overall_accuracy": entry["overall_accuracy"],
                "correct": entry["correct"],
                "total": entry["total"],
                "by_symbol": entry.get("by_symbol", {}),
                "degraded_symbols": entry.get("degraded_symbols", []),
            })
    
    if not snapshots:
        return {
            "trading_type": trading_type or "all",
            "days": days,
            "snapshots": [],
            "message": f"No snapshots in the last {days} days"
        }
    
    # Calculate statistics
    accuracy_values = [s["overall_accuracy"] for s in snapshots]
    
    statistics = {
        "min_accuracy": round(min(accuracy_values), 3),
        "max_accuracy": round(max(accuracy_values), 3),
        "avg_accuracy": round(sum(accuracy_values) / len(accuracy_values), 3),
        "latest_accuracy": round(snapshots[-1]["overall_accuracy"], 3),
        "snapshots_count": len(snapshots),
    }
    
    # Degradation alert: current accuracy is >10% below peak
    peak_accuracy = max(accuracy_values)
    current_accuracy = snapshots[-1]["overall_accuracy"]
    degradation_pct = ((peak_accuracy - current_accuracy) / peak_accuracy * 100) if peak_accuracy > 0 else 0
    
    degradation_alert = {
        "is_degraded": degradation_pct > 10.0,
        "degradation_pct": round(degradation_pct, 1),
        "peak_accuracy": round(peak_accuracy, 3),
        "current_accuracy": round(current_accuracy, 3),
        "recommendation": (
            f"RETRAIN MODELS — accuracy dropped {degradation_pct:.0f}% from peak"
            if degradation_pct > 10.0
            else "Accuracy stable"
        ),
    }
    
    return {
        "trading_type": trading_type or "all",
        "days": days,
        "snapshots": snapshots,
        "current": snapshots[-1] if snapshots else None,
        "statistics": statistics,
        "degradation_alert": degradation_alert,
    }


@router.get("/lstm/confidence-distribution")
def get_confidence_distribution(
    trading_type: Optional[str] = Query(None),
    min_samples: int = Query(20),
):
    """
    Return distribution of LSTM confidence predictions.
    
    Shows how often the model predicts at each confidence level.
    Useful for detecting if model is "stuck" always predicting same confidence,
    or if predictions are well-distributed across the range.
    
    Returns:
    - histogram: List of {confidence_range, count, percentage}
    - statistics: mean, median, std_dev, min, max confidence
    - balance: Check if predictions are concentrated in narrow range
    """
    from ai.trade_memory import memory
    
    # Get trades with confidence predictions
    outcomes = memory.recent(
        n=memory.MAX_BUFFER,
        trading_type=trading_type,
        live_only=True,
    )
    
    # Filter for trades with confidence > 0
    tracked = [o for o in outcomes if o.get("confidence", 0) > 0]
    
    if len(tracked) < min_samples:
        return {
            "tracked": len(tracked),
            "min_samples": min_samples,
            "sufficient_data": False,
            "message": f"Need at least {min_samples} trades with confidence data"
        }
    
    confidences = [o.get("confidence", 0) for o in tracked]
    
    # Create histogram bins (5% increments: 40-45%, 45-50%, ..., 85-90%)
    bins = [(i * 0.05, (i + 1) * 0.05) for i in range(8, 18)]  # 0.40 to 0.90
    histogram = []
    
    for bin_start, bin_end in bins:
        count = sum(1 for c in confidences if bin_start <= c < bin_end)
        percentage = (count / len(tracked)) * 100 if len(tracked) > 0 else 0.0
        
        histogram.append({
            "confidence_range": f"{bin_start:.0%}-{bin_end:.0%}",
            "bin_start": round(bin_start, 2),
            "bin_end": round(bin_end, 2),
            "count": count,
            "percentage": round(percentage, 1),
        })
    
    # Calculate statistics
    import statistics
    mean_conf = statistics.mean(confidences)
    median_conf = statistics.median(confidences)
    std_dev = statistics.stdev(confidences) if len(confidences) > 1 else 0.0
    min_conf = min(confidences)
    max_conf = max(confidences)
    conf_range = max_conf - min_conf
    
    # Balance check: if >50% of predictions in a single 5% bin, model is too concentrated
    max_bin_percentage = max(h["percentage"] for h in histogram)
    is_well_distributed = max_bin_percentage < 50.0 and conf_range > 0.15
    
    return {
        "histogram": histogram,
        "statistics": {
            "mean": round(mean_conf, 3),
            "median": round(median_conf, 3),
            "std_dev": round(std_dev, 3),
            "min": round(min_conf, 3),
            "max": round(max_conf, 3),
            "range": round(conf_range, 3),
        },
        "balance": {
            "is_well_distributed": is_well_distributed,
            "max_bin_percentage": round(max_bin_percentage, 1),
            "recommendation": (
                "Good distribution" if is_well_distributed
                else f"Model concentrated at {mean_conf:.0%} (consider recalibration)"
            ),
        },
        "total_trades": len(tracked),
        "trading_type": trading_type or "all",
    }


@router.get("/rl/status")
def rl_status():
    """Return current RL agent parameters for all trading types."""
    return rl_manager.status()


@router.get("/rl/history/{trading_type}")
def get_rl_history(
    trading_type: TRADING_TYPE,
    days: int = Query(7, ge=1, le=90),
):
    """
    Return time-series history of RL threshold evolution for a trading type.
    
    Tracks how conf_thresh and risk_factor have changed over time as the RL agent learns
    from trade outcomes. Useful for visualizing learning progress and detecting instability.
    
    Args:
        trading_type: scalping | day_trading | swing
        days: Number of days of history to return (1-90, default 7)
    
    Returns:
        - snapshots: List of {timestamp, conf_thresh, risk_factor, n_updates, state}
        - current: Latest snapshot
        - statistics: min/max/avg threshold values, update count
        - trend: Direction and magnitude of threshold changes
    """
    from pathlib import Path
    from datetime import datetime, timedelta, timezone
    import json
    
    # Read account mode
    _mode_path = Path("config/account_mode.json")
    if not _mode_path.exists():
        return {"error": "account_mode.json not found", "snapshots": []}
    
    with open(_mode_path, encoding="utf-8") as f:
        _mode_data = json.load(f)
        _mode = _mode_data.get("mode", "paper")
    
    _history_path = Path("ai/data") / f"rl_history_{trading_type}_{_mode}.jsonl"
    
    if not _history_path.exists():
        return {
            "trading_type": trading_type,
            "mode": _mode,
            "snapshots": [],
            "message": f"No history file found for {trading_type}/{_mode}"
        }
    
    # Read JSONL history file
    snapshots = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    
    with open(_history_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            # Parse timestamp (strip trailing Z, already has timezone offset)
            ts = datetime.fromisoformat(entry["timestamp"].rstrip("Z"))
            if ts < cutoff:
                continue  # Skip entries older than requested days
            
            snapshots.append({
                "timestamp": entry["timestamp"],
                "conf_thresh": entry["conf_thresh"],
                "risk_factor": entry["risk_factor"],
                "n_updates": entry["n_updates"],
                "last_state": entry.get("last_state", "unknown"),
            })
    
    if not snapshots:
        return {
            "trading_type": trading_type,
            "mode": _mode,
            "days": days,
            "snapshots": [],
            "message": f"No snapshots in the last {days} days"
        }
    
    # Calculate statistics
    conf_values = [s["conf_thresh"] for s in snapshots]
    risk_values = [s["risk_factor"] for s in snapshots]
    
    statistics = {
        "conf_thresh": {
            "min": round(min(conf_values), 3),
            "max": round(max(conf_values), 3),
            "avg": round(sum(conf_values) / len(conf_values), 3),
            "range": round(max(conf_values) - min(conf_values), 3),
        },
        "risk_factor": {
            "min": round(min(risk_values), 3),
            "max": round(max(risk_values), 3),
            "avg": round(sum(risk_values) / len(risk_values), 3),
            "range": round(max(risk_values) - min(risk_values), 3),
        },
        "total_updates": snapshots[-1]["n_updates"] if snapshots else 0,
        "snapshots_count": len(snapshots),
    }
    
    # Trend analysis (first vs last snapshot)
    if len(snapshots) >= 2:
        first = snapshots[0]
        last = snapshots[-1]
        trend = {
            "conf_thresh_change": round(last["conf_thresh"] - first["conf_thresh"], 3),
            "risk_factor_change": round(last["risk_factor"] - first["risk_factor"], 3),
            "updates_delta": last["n_updates"] - first["n_updates"],
            "time_span_days": round((
                datetime.fromisoformat(last["timestamp"].replace("Z", "+00:00")) -
                datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00"))
            ).total_seconds() / 86400, 2),
        }
    else:
        trend = None
    
    return {
        "trading_type": trading_type,
        "mode": _mode,
        "days": days,
        "snapshots": snapshots,
        "current": snapshots[-1] if snapshots else None,
        "statistics": statistics,
        "trend": trend,
    }


@router.get("/rl/win-rate-by-state/{trading_type}")
def get_win_rate_by_state(
    trading_type: TRADING_TYPE,
    min_samples: int = Query(5, ge=1),
):
    """
    Return win rate breakdown by RL state bucket.
    
    Shows which market conditions (win_rate bucket, confidence bucket, session, drawdown, volatility)
    are most profitable, helping identify when to be aggressive vs defensive.
    
    Requires rl_state field to be populated in trade_memory.jsonl.
    
    Args:
        trading_type: scalping | day_trading | swing
        min_samples: Minimum trades per state to include (default 5)
    
    Returns:
        - states: List of {state, win_rate, avg_profit, samples, state_components}
        - best_states: Top 5 states by win rate
        - worst_states: Bottom 5 states by win rate
        - total_states: Number of unique states observed
    """
    from ai.trade_memory import memory
    
    # Get trades for this trading type
    outcomes = memory.recent(
        n=memory.MAX_BUFFER,
        trading_type=trading_type,
        live_only=True,
    )
    
    # Filter for trades with rl_state populated
    tracked = [o for o in outcomes if o.get("rl_state")]
    
    if len(tracked) < min_samples:
        return {
            "trading_type": trading_type,
            "tracked": len(tracked),
            "min_samples": min_samples,
            "sufficient_data": False,
            "message": f"Need at least {min_samples} trades with rl_state data"
        }
    
    # Aggregate by state
    state_stats = {}
    for o in tracked:
        state = o.get("rl_state", "unknown")
        profit = o.get("profit", 0)
        is_win = profit > 0
        
        if state not in state_stats:
            state_stats[state] = {
                "wins": 0,
                "losses": 0,
                "total_profit": 0.0,
                "total_pips": 0.0,
            }
        
        state_stats[state]["wins" if is_win else "losses"] += 1
        state_stats[state]["total_profit"] += profit
        state_stats[state]["total_pips"] += o.get("profit_pips", 0)
    
    # Build result list
    states_list = []
    for state, stats in state_stats.items():
        samples = stats["wins"] + stats["losses"]
        if samples < min_samples:
            continue  # Skip states with insufficient data
        
        win_rate = stats["wins"] / samples if samples > 0 else 0.0
        avg_profit = stats["total_profit"] / samples if samples > 0 else 0.0
        avg_pips = stats["total_pips"] / samples if samples > 0 else 0.0
        
        # Parse state components (e.g. "med_high_active_low_tight")
        components = state.split("_")
        state_breakdown = {
            "win_rate_bucket": components[0] if len(components) > 0 else "unknown",
            "conf_bucket": components[1] if len(components) > 1 else "unknown",
            "session": components[2] if len(components) > 2 else "unknown",
            "drawdown_bucket": components[3] if len(components) > 3 else "unknown",
            "volatility_bucket": components[4] if len(components) > 4 else "unknown",
        }
        
        states_list.append({
            "state": state,
            "win_rate": round(win_rate, 3),
            "avg_profit": round(avg_profit, 2),
            "avg_pips": round(avg_pips, 1),
            "samples": samples,
            "wins": stats["wins"],
            "losses": stats["losses"],
            "state_components": state_breakdown,
        })
    
    # Sort by win rate descending
    states_list.sort(key=lambda x: x["win_rate"], reverse=True)
    
    # Identify best and worst states — ensure no overlap between the two lists
    half = max(1, len(states_list) // 2)
    best_states = states_list[:min(5, half)]
    best_keys = {s["state"] for s in best_states}
    worst_candidates = [s for s in states_list if s["state"] not in best_keys]
    worst_states = worst_candidates[-5:][::-1] if worst_candidates else []
    
    return {
        "trading_type": trading_type,
        "states": states_list,
        "best_states": best_states,
        "worst_states": worst_states,
        "total_states": len(states_list),
        "total_trades": len(tracked),
    }


@router.get("/rl/qtable/{trading_type}")
def get_rl_qtable(
    trading_type: TRADING_TYPE,
    strategy_name: Optional[str] = Query(None, description="Specific strategy (e.g. sr_breakout). Omit for trading-type fallback."),
):
    """
    Return full Q-table for a specific strategy/trading type with state breakdown.

    Returns:
    - q_values: Dict of state → [Q-values for each action]
    - states_breakdown: List of states with parsed components
    - actions: Action definitions [(conf_delta, risk_delta), ...]
    - current_params: Current conf_thresh, risk_factor, n_updates
    - statistics: Min/max Q-values, state coverage
    """
    from ai.rl_agent import ACTIONS

    agent = rl_manager.agent(strategy_name, trading_type)
    
    # Get Q-table (protected by lock in status())
    agent_status = agent.status()
    
    # Parse states into components for filtering/visualization
    states_breakdown = []
    q_values_dict = {}
    
    # Access the Q-table directly (it's already a dict)
    with agent._lock:
        q_table = dict(agent._q)
    
    for state_name, q_vals in q_table.items():
        # Parse state name: "wr_conf_session_drawdown_vol"
        parts = state_name.split("_")
        if len(parts) >= 5:
            states_breakdown.append({
                "state": state_name,
                "win_rate_bucket": parts[0],      # low/med/high
                "conf_bucket": parts[1],          # low/med/high
                "session": parts[2],              # quiet/active/overlap
                "drawdown_bucket": parts[3],      # low/med/high
                "volatility": parts[4],           # tight/wide
                "q_values": q_vals,
                "max_q": max(q_vals) if q_vals else 0.0,
                "best_action": q_vals.index(max(q_vals)) if q_vals else 4,  # 4 = hold
            })
            q_values_dict[state_name] = q_vals
    
    # Calculate statistics
    all_q_values = [q for qs in q_values_dict.values() for q in qs]
    min_q = min(all_q_values) if all_q_values else 0.0
    max_q = max(all_q_values) if all_q_values else 0.0
    avg_q = sum(all_q_values) / len(all_q_values) if all_q_values else 0.0
    
    return {
        "trading_type": trading_type,
        "mode": agent._mode,
        "q_values": q_values_dict,
        "states_breakdown": states_breakdown,
        "actions": [
            {
                "index": i,
                "conf_delta": action[0],
                "risk_delta": action[1],
                "label": f"Conf {action[0]:+.2f}, Risk {action[1]:+.2f}" if action != (0.0, 0.0) else "Hold"
            }
            for i, action in enumerate(ACTIONS)
        ],
        "current_params": {
            "conf_thresh": agent_status["confidence_threshold"],
            "risk_factor": agent_status["risk_factor"],
            "n_updates": agent_status["n_updates"],
            "last_state": agent_status["last_state"],
            "epsilon": agent_status["epsilon"],
        },
        "statistics": {
            "total_states": len(q_values_dict),
            "total_state_action_pairs": len(all_q_values),
            "min_q_value": round(min_q, 4),
            "max_q_value": round(max_q, 4),
            "avg_q_value": round(avg_q, 4),
        }
    }


@router.post("/rl/reset/{trading_type}")
def rl_reset(
    trading_type: TRADING_TYPE,
    strategy_name: Optional[str] = Query(None, description="Specific strategy to reset. Omit to reset all strategies for this trading type."),
):
    """Reset one (or all) RL agent(s) for a trading type back to default thresholds."""
    import os
    from ai.rl_agent import DATA_DIR, RLAgent, _ALL_STRATEGIES
    from engine.account_store import current_mode as _cm
    _mode = _cm()

    strategies_to_reset = (
        [strategy_name]
        if strategy_name
        else _ALL_STRATEGIES.get(trading_type, [])
    )
    reset_keys = []
    for strat in strategies_to_reset:
        key = f"{strat}_{trading_type}"
        path = DATA_DIR / f"rl_qtable_{strat}_{trading_type}_{_mode}.json"
        if path.exists():
            os.remove(path)
        rl_manager._agents[key] = RLAgent(trading_type, mode=_mode, strategy_name=strat)
        reset_keys.append(key)
    return {"status": "reset", "trading_type": trading_type, "strategies_reset": reset_keys, "mode": _mode}


# ───────────────────────────────────
# Trade Memory endpoints
# ───────────────────────────────────
@router.get("/memory/drift")
def memory_drift_detection(
    trading_type: Optional[TRADING_TYPE] = None,
    window: int = Query(30),
):
    """Detect win rate drift using Page-Hinkley test."""
    return memory.detect_drift(
        trading_type=trading_type,
        window=window,
        live_only=True,
    )

@router.get("/memory/stability")
def memory_ev_stability(
    trading_type: Optional[TRADING_TYPE] = None,
    window: int = Query(20),
):
    """Return rolling EV stability trend across time windows."""
    return memory.rolling_ev_stability(
        trading_type=trading_type,
        window=window,
        live_only=True,
    )

@router.get("/memory/stats")
def memory_stats(trading_type: Optional[TRADING_TYPE] = None):
    """Return aggregate win rate, avg P&L, SL/TP hit counts."""
    return memory.stats(trading_type=trading_type)

@router.post("/memory/reload")
def reload_trade_memory():
    """Reload trade_memory.jsonl from disk into the in-memory buffer.
    Use after external edits (e.g. backfill scripts) without restarting the API."""
    count = memory.reload()
    return {"status": "ok", "entries_loaded": count}

@router.get("/memory/stats/regime")
def memory_stats_by_regime(
    trading_type: Optional[TRADING_TYPE] = None,
    min_samples: int = Query(5),
):
    """Return win rate and avg PnL broken down by market regime."""
    return memory.stats_by_regime(
        trading_type=trading_type,
        min_samples=min_samples,
        live_only=True,
    )

@router.get("/memory/recent")
def memory_recent(n: int = 50, trading_type: Optional[TRADING_TYPE] = None):
    """Return the last N trade outcomes, newest last."""
    return memory.recent(n=min(n, 500), trading_type=trading_type)


# ── LSTM Prediction Cache ───────────────────────────────────────────────────

@router.get("/lstm/cache/stats")
def get_cache_stats():
    """
    Return prediction cache statistics.
    Cache stores LSTM predictions for 5 minutes to reduce redundant computation.
    """
    return predictor.cache_stats()

@router.post("/lstm/cache/clear")
def clear_cache(symbol: str = None, trading_type: str = None):
    """
    Clear prediction cache. If symbol/trading_type provided, clear only that entry.
    Otherwise clear all cached predictions.
    """
    cleared = predictor.clear_cache(symbol, trading_type)
    return {
        "status": "cleared",
        "entries_cleared": cleared,
        "scope": "all" if not symbol else f"{symbol}_{trading_type}",
    }


# ───────────────────────────────────
# Parameter Optimizer endpoints
# ───────────────────────────────────

from ai.param_optimizer import optimizer as _optimizer, PARAM_GRIDS, MAX_CONCURRENT_OPT


@router.get("/optimizer/status")
def optimizer_status():
    """Return current optimizer status for all (strategy, symbol) pairs."""
    return {
        "jobs":        _optimizer.status(),
        "param_grids": {k: list(v.keys()) for k, v in PARAM_GRIDS.items()},
    }


# Bars to fetch per trading type — matches run_optimizer.py standalone values.
# These cover 2+ years of history so UI-triggered optimizations are consistent
# with the standalone run and will NOT downgrade already-optimized results.
_OPT_BARS: dict[str, int] = {
    "scalping":    200_000,   # M5  ~2 years
    "day_trading":  50_000,   # H1  ~5.7 years
    "swing":         30_000,   # H4  ~13.7 years
}


class OptimizeRequest(BaseModel):
    trading_type: TRADING_TYPE = "day_trading"
    bars: int = 0  # 0 = auto (uses _OPT_BARS[trading_type]); set explicitly to override


@router.post("/optimizer/run/{strategy_name}/{symbol}")
async def run_optimizer(
    strategy_name: str,
    symbol: str,
    req: OptimizeRequest = OptimizeRequest(),
):
    """
    Trigger backtest grid-search optimization for one (strategy, symbol) pair.
    Runs in background — poll GET /ai/optimizer/status to track progress.
    """
    from api.main import get_mt5_client
    from datetime import datetime, timezone, timedelta

    if strategy_name not in PARAM_GRIDS:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    tf_str  = TRADING_TYPE_TF.get(req.trading_type, "H1")
    n_bars  = req.bars or _OPT_BARS.get(req.trading_type, 17_000)
    # Scalping: use date-range fetch to get full 2-year M5 dataset
    if req.trading_type == "scalping" and req.bars == 0:
        _date_to   = datetime.now(tz=timezone.utc)
        _date_from = _date_to - timedelta(days=694)
        df = await asyncio.to_thread(client.get_ohlcv_range, symbol, tf_str, _date_from, _date_to)
    else:
        df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, n_bars)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {symbol}/{tf_str}")

    started = _optimizer.optimize_async(strategy_name, symbol, df, req.trading_type)
    return {
        "status":         "optimization_started" if started else "already_running",
        "strategy":       strategy_name,
        "symbol":         symbol,
        "trading_type":   req.trading_type,
        "bars":           len(df),
    }


@router.post("/optimizer/run/all")
async def run_optimizer_all(req: OptimizeRequest = OptimizeRequest()):
    """
    Queues optimization for every (strategy, enabled-symbol) pair and returns
    immediately.  A background task does all MT5 fetching + dispatching in
    batched steps with deliberate rest intervals so the live bot is never
    starved of the MT5 lock:

        for each mode  (scalping → day_trading → swing)
            for each pair  → fetch OHLCV + sleep 300 ms
            sleep 2 s      ← mode boundary rest
            for each strategy
                for each pair  → dispatch optimizer thread + sleep 100 ms
                sleep 1 s      ← strategy boundary rest
    """
    from api.main import get_mt5_client
    from datetime import datetime, timezone, timedelta
    import json

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    CONFIG_PATH = Path(__file__).parent.parent.parent / "config"
    try:
        symbols_cfg    = json.loads((CONFIG_PATH / "symbols.json").read_text(encoding="utf-8-sig"))
        strategies_cfg = json.loads((CONFIG_PATH / "strategies.json").read_text(encoding="utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=500, detail="Cannot read config files")
    try:
        scanner_cfg = json.loads((CONFIG_PATH / "scanner.json").read_text(encoding="utf-8-sig"))
    except Exception:
        scanner_cfg = {}

    async def _bg_task() -> None:
        ohlcv_cache: dict[tuple[str, str], object] = {}

        for trading_type in ("scalping", "day_trading", "swing"):
            active   = strategies_cfg.get(trading_type, {}).get("active_strategies", [])
            tf_str   = TRADING_TYPE_TF.get(trading_type, "H1")
            sym_list = symbols_cfg.get(trading_type, [])
            base_syms = [
                (e.get("symbol") if isinstance(e, dict) else e)
                for e in sym_list
                if (e.get("enabled", False) if isinstance(e, dict) else True)
            ]
            # Include symbols discovered by the market scanner
            scanner_syms = [
                s for s in scanner_cfg.get(trading_type, {}).get("symbols", [])
                if isinstance(s, str) and s
            ]
            seen: set[str] = set()
            symbols: list[str] = []
            for s in base_syms + scanner_syms:
                if s and s not in seen:
                    seen.add(s)
                    symbols.append(s)

            # ── Phase 1: fetch each (symbol, tf) once, 300 ms between pairs ──
            n_bars = req.bars or _OPT_BARS.get(trading_type, 17_000)
            for symbol in symbols:
                if not symbol:
                    continue
                cache_key = (symbol, tf_str)
                if cache_key not in ohlcv_cache:
                    # Scalping: use date-range fetch to get full 2-year M5 dataset
                    if trading_type == "scalping" and req.bars == 0:
                        _date_to   = datetime.now(tz=timezone.utc)
                        _date_from = _date_to - timedelta(days=694)
                        df = await asyncio.to_thread(
                            client.get_ohlcv_range, symbol, tf_str, _date_from, _date_to
                        )
                    else:
                        df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, n_bars)
                    ohlcv_cache[cache_key] = df
                await asyncio.sleep(0.3)  # rest between pairs — gives live bot MT5 lock

            # ── Mode boundary rest ─────────────────────────────────────────────
            await asyncio.sleep(2.0)

            # ── Phase 2: dispatch optimizer threads, 100 ms between dispatches ──
            for strat in active:
                if strat not in PARAM_GRIDS:
                    continue
                for symbol in symbols:
                    if not symbol:
                        continue
                    df = ohlcv_cache.get((symbol, tf_str))
                    if df is None or df.empty:
                        continue
                    # Wait for a free concurrency slot before dispatching
                    while sum(1 for j in _optimizer.status().values() if j.get("running")) >= MAX_CONCURRENT_OPT:
                        await asyncio.sleep(5.0)
                    _optimizer.optimize_async(strat, symbol, df, trading_type)
                    await asyncio.sleep(0.1)  # rest between pairs

                # ── Strategy boundary rest ─────────────────────────────────────
                await asyncio.sleep(1.0)

    asyncio.create_task(_bg_task())
    return {"status": "queued", "detail": "Optimizer running in background — poll /ai/optimizer/status"}

