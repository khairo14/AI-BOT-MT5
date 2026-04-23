"""
Standalone LSTM Retrainer
==========================
Connects to MT5, fetches 2 years of OHLCV data for every active
symbol × trading-type pair, and retrains each LSTM price-direction model.

After all models have been retrained, the script validates accuracy and
— if ≥ 60% of day_trading + swing models achieve accuracy > 0.60 —
automatically updates the AI scorer weights in config/app.json:

  day_trading / swing:
    lstm  0.30 → 0.45   (now trustworthy with 2-year training set)
    rr    0.35 → 0.25
    volume 0.15 → 0.10
    trend  stays 0.20

  scalping: unchanged (M5 LSTM less reliable than H1/H4)

Usage (from workspace root):
    python -m ai.run_retrain
    python ai/run_retrain.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

from engine.mt5_client import MT5Client
from ai.predictor import predictor as global_predictor

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Dispatch limit per trading type — caps how many jobs are queued at once.
# Actual GPU concurrency is controlled by predictor._executor (max_workers=2),
# so setting this higher than 2 just pre-fills the queue — it does not run more
# jobs simultaneously. Keep scalping at 2 to avoid saturating RAM queue;
# day/swing are small enough to queue freely.
_MAX_CONCURRENT: dict[str, int] = {
    "scalping":    2,   # 200K M5 bars — queue conservatively
    "day_trading": 2,   # matches executor max_workers (no point queuing more)
    "swing":       2,   # same
}

_BARS: dict[str, int] = {
    "scalping":     200_000,   # M5:  ~2 years
    "day_trading":  50_000,   # H1:  ~5.71 years
    "swing":         30_000,   # H4:  ~13.7 years
}
_SKIP_SCALPING = False
_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

# Accuracy threshold — update scorer weights only if this fraction of
# day_trading + swing models exceed 0.60 accuracy.
_ACCURACY_UPGRADE_THRESHOLD = 0.60
_MIN_FRACTION_ABOVE          = 0.60   # 60% of models must pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_jobs(config_dir: Path) -> list[tuple[str, str]]:
    """Return (symbol, trading_type) pairs from:
    1. config/symbols.json  — static baseline symbols
    2. config/scanner.json  — scanner-discovered symbols currently active per mode
    Union ensures LSTM models exist for every symbol the bot may actually trade.
    """
    symbols_cfg = json.loads((config_dir / "symbols.json").read_text(encoding="utf-8"))
    jobs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for trading_type in ("scalping", "day_trading", "swing"):
        if _SKIP_SCALPING and trading_type == "scalping":
            logger.info("Skipping scalping — run_retrain_scalping.py handles these")
            continue
        for entry in symbols_cfg.get(trading_type, []):
            if entry.get("enabled", True):
                key = (entry["symbol"], trading_type)
                if key not in seen:
                    jobs.append(key)
                    seen.add(key)

    # Also include scanner-discovered symbols so models exist for them too
    scanner_path = config_dir / "scanner.json"
    if scanner_path.exists():
        try:
            scanner_cfg = json.loads(scanner_path.read_text(encoding="utf-8"))
            for trading_type in ("scalping", "day_trading", "swing"):
                if _SKIP_SCALPING and trading_type == "scalping":
                    continue
                for sym in scanner_cfg.get(trading_type, {}).get("symbols", []):
                    if sym:
                        key = (sym, trading_type)
                        if key not in seen:
                            jobs.append(key)
                            seen.add(key)
        except Exception as exc:
            logger.warning(f"Could not read scanner.json for symbol list: {exc}")

    return jobs


def _wait_batch(active_keys: set[str]) -> set[str]:
    """Return keys that finished since last check (non-blocking poll)."""
    return {
        k for k in active_keys
        if not global_predictor.is_training(*k.split("__", 1))
    }


def _get_accuracy_stats(models_dir: Path) -> dict[str, float]:
    """Read all *_meta.json files and return {symbol__trading_type: accuracy}."""
    stats: dict[str, float] = {}
    for f in models_dir.glob("*_meta.json"):
        try:
            meta = json.loads(f.read_text(encoding="utf-8"))
            name = f.stem.replace("_meta", "")
            # Match trading type suffix first to correctly handle symbols
            # with underscores in their name (e.g. US100Cash_day_trading)
            key = None
            for tt in ("day_trading", "swing", "scalping"):
                if name.endswith(f"_{tt}"):
                    symbol = name[:-(len(tt) + 1)]
                    key    = f"{symbol}__{tt}"
                    break
            if key is None:
                key = name.replace("_", "__", 1)
            stats[key] = float(meta.get("accuracy", 0.0))
        except Exception as exc:
            logger.debug(f"Could not read {f}: {exc}")
    return stats


def _update_scorer_weights(config_dir: Path, stats: dict[str, float]) -> bool:
    """
    Update AI scorer weights in config/app.json if the new models are good enough.
    Returns True if weights were updated.
    """
    day_swing_accs = [
        acc for key, acc in stats.items()
        if "__day_trading" in key or "__swing" in key
    ]
    if not day_swing_accs:
        logger.warning("No day_trading/swing accuracy stats — scorer weights unchanged.")
        return False

    above_60    = sum(1 for a in day_swing_accs if a > _ACCURACY_UPGRADE_THRESHOLD)
    frac_above  = above_60 / len(day_swing_accs)
    med_acc     = sorted(day_swing_accs)[len(day_swing_accs) // 2]

    logger.info(
        f"Day/Swing accuracy: median={med_acc:.4f}, "
        f"{above_60}/{len(day_swing_accs)} ({frac_above:.0%}) above 0.60"
    )

    if frac_above < _MIN_FRACTION_ABOVE:
        logger.warning(
            f"Only {frac_above:.0%} of models exceed 0.60 accuracy "
            f"(need {_MIN_FRACTION_ABOVE:.0%}) — scorer weights NOT updated."
        )
        return False

    app_file = config_dir / "app.json"
    cfg = json.loads(app_file.read_text(encoding="utf-8-sig"))

    # Day trading + swing: raise LSTM weight, reduce RR and volume weights
    cfg["ai"]["scorer_weights"] = {
        "lstm":   0.45,
        "rr":     0.25,
        "trend":  0.20,
        "volume": 0.10,
    }

    # Proportionally rescale per-regime LSTM weights (raise each toward 0.45
    # while preserving the relative ratios of rr / trend / volume).
    for regime, w in cfg["ai"].get("regime_weights", {}).items():
        old_lstm    = w.get("lstm", 0.30)
        others_sum  = 1.0 - old_lstm
        if others_sum <= 0:
            continue
        scale = (1.0 - 0.45) / others_sum
        w["lstm"] = 0.45
        for k in ("rr", "trend", "volume"):
            if k in w:
                w[k] = round(w[k] * scale, 4)

    app_file.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(
        "Scorer weights updated in config/app.json  "
        "(day_trading/swing lstm 0.30→0.45, rr 0.35→0.25, volume 0.15→0.10)"
    )
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config_dir = ROOT / "config"
    models_dir = ROOT / "ai" / "models"

    logger.info("=" * 70)
    logger.info("AI-BOT-MT5  LSTM Retrainer  (standalone run)")
    logger.info("=" * 70)

    jobs = _build_jobs(config_dir)
    logger.info(f"Total pairs to train: {len(jobs)}")

    # ── Phase 1: fetch all data upfront ──────────────────────────────────────
    logger.info("\nPhase 1 — Fetching 2-year historical data …")
    data_cache: dict[tuple[str, str], Optional[pd.DataFrame]] = {}

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        seen: set[tuple[str, str]] = set()
        for symbol, trading_type in jobs:
            key = (symbol, trading_type)
            if key in seen:
                continue
            seen.add(key)

            tf    = _TF[trading_type]
            count = _BARS[trading_type]
            df    = client.get_ohlcv(symbol, tf, count=count)
            data_cache[key] = df

            bar_count = len(df) if df is not None else 0
            status_tag = f"{bar_count:>8,d} bars" if df is not None else "  NO DATA"
            logger.info(f"  {symbol:20s} {tf:3s} → {status_tag}")

    # ── Phase 2: train all models (no MT5 needed) ────────────────────────────
    logger.info("\nPhase 2 — Training LSTMs (max 2 concurrent workers across all types) …")

    total_submitted = 0
    skipped         = 0
    # Track active jobs by trading_type for dynamic throttling
    active_by_type: dict[str, set[str]] = {
        "scalping":    set(),
        "day_trading": set(),
        "swing":       set(),
    }

    for idx, (symbol, trading_type) in enumerate(jobs, 1):
        df = data_cache.get((symbol, trading_type))
        if df is None:
            logger.warning(f"  [{idx:>2d}] {symbol}/{trading_type}: no data — skipped")
            skipped += 1
            continue

        # Throttle: wait until a concurrency slot is free for this trading_type
        max_for_type = _MAX_CONCURRENT.get(trading_type, 6)
        active_keys = active_by_type[trading_type]
        while len(active_keys) >= max_for_type:
            done = _wait_batch(active_keys)
            active_keys -= done
            for k in done:
                sym, tt = k.split("__", 1)
                logger.info(f"  ✓ finished {sym}/{tt}")
            if not done:
                time.sleep(5.0)

        job_key = f"{symbol}__{trading_type}"
        if global_predictor.train_async(symbol, df, trading_type):
            active_by_type[trading_type].add(job_key)
            total_submitted += 1
            logger.info(
                f"  [{idx:>2d}/{len(jobs)}] started   {symbol}/{trading_type} "
                f"({len(df):,} bars)"
            )
        else:
            logger.warning(
                f"  [{idx:>2d}] {symbol}/{trading_type}: already training — skipped"
            )

    # Drain remaining active jobs
    all_active = set()
    for keys in active_by_type.values():
        all_active |= keys
    logger.info(f"\nWaiting for final {len(all_active)} job(s) …")
    while all_active:
        done = _wait_batch(all_active)
        all_active -= done
        for k in done:
            sym, tt = k.split("__", 1)
            logger.info(f"  ✓ finished {sym}/{tt}")
        if all_active:
            time.sleep(10.0)

    # ── Phase 3: validate accuracy + update scorer weights ───────────────────
    logger.info("\nPhase 3 — Validating accuracy …")
    stats = _get_accuracy_stats(models_dir)

    above_60 = sum(1 for a in stats.values() if a > 0.60)
    logger.info(f"{above_60}/{len(stats)} models achieved accuracy > 0.60")
    for key in sorted(stats.keys()):
        marker = "✓" if stats[key] > 0.60 else "✗"
        logger.info(f"  {marker} {key:40s}  acc={stats[key]:.4f}")

    logger.info("\n" + "=" * 70)
    logger.info(
        f"Retraining complete — "
        f"{total_submitted} trained  |  {skipped} skipped"
    )
    logger.info("=" * 70)

    # ── Phase 4: re-fit Platt scaling calibration ─────────────────────────────
    # Now that models are retrained the old calibration params are stale.
    # Re-fit using live trade outcomes stored in trade_memory (requires ≥30 trades
    # per symbol+type). Runs in-process — fast (seconds per model).
    logger.info("\nPhase 4 — Re-fitting Platt scaling calibration …")
    try:
        cal_results: list[str] = []
        for symbol, trading_type in jobs:
            result = global_predictor.calibrate(symbol, trading_type)
            status = result.get("status", "unknown")
            if status == "calibrated":
                cal_results.append(
                    f"  ✓ {symbol}/{trading_type}  a={result['a']}  b={result['b']}  "
                    f"n={result['samples']}"
                )
            elif status == "insufficient_data":
                cal_results.append(
                    f"  – {symbol}/{trading_type}  skipped ({result['samples']}/{result['needed']} trades)"
                )
            else:
                cal_results.append(f"  ✗ {symbol}/{trading_type}  {status}")
        for line in cal_results:
            logger.info(line)
        calibrated = sum(1 for r in cal_results if r.startswith("  ✓"))
        logger.info(f"Calibration complete — {calibrated}/{len(jobs)} models re-fitted")
    except Exception as exc:
        logger.warning(f"Calibration phase failed: {exc}")


if __name__ == "__main__":
    main()
