"""
Scalping-Only LSTM Retrainer
=============================
Fetches 2 years of M5 OHLCV data for every enabled scalping symbol
and retrains each LSTM model in parallel (GPU-accelerated).

Key differences from run_retrain.py:
  - Scalping only — day_trading and swing are untouched
  - Uses get_ohlcv_range() with a 2-year date window instead of
    get_ohlcv() which is capped at ~99k bars (~1 year for M5)
  - Higher MAX_CONCURRENT_TRAIN since all jobs are the same type
    and your RTX 4060 can handle more parallel M5 training jobs
  - Resets scalping RL Q-table after successful retraining so the
    agent starts fresh with the new model quality baseline

Usage (from workspace root, venv activated):
    python ai/run_retrain_scalping.py

    Optional flags:
    --dry-run     Print symbols and bar counts without training
    --no-rl-reset Skip the RL Q-table reset after training
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

from engine.mt5_client import MT5Client
from ai.predictor import predictor as global_predictor

# ── Configuration ────────────────────────────────────────────────────────────

# RTX 4060 can handle more concurrent M5 jobs than the default 4
# since all jobs are the same size (~210k bars) and same architecture.
# Increase if GPU VRAM allows; decrease if you see OOM errors.
MAX_CONCURRENT_TRAIN = 6

# 2-year lookback for M5 — covers more market regimes than 1-year
# which prevents the LSTM from overfitting to recent trends and
# producing inflated confidence scores that push RL threshold to ceiling.
LOOKBACK_YEARS = 2

# Minimum accuracy to consider a retraining run successful enough
# to trigger RL reset. If fewer than this fraction of models pass
# the 0.58 accuracy gate, the RL table is NOT reset.
_MIN_SUCCESS_FRACTION = 0.50   # at least 50% of models must save successfully


# ── Helpers ──────────────────────────────────────────────────────────────────

def _build_scalping_jobs(config_dir: Path) -> list[str]:
    """Return list of enabled scalping symbols from config/symbols.json."""
    symbols_cfg = json.loads(
        (config_dir / "symbols.json").read_text(encoding="utf-8")
    )
    jobs: list[str] = []
    for entry in symbols_cfg.get("scalping", []):
        if entry.get("enabled", True):
            jobs.append(entry["symbol"])
    return jobs


def _wait_batch(active_keys: set[str]) -> set[str]:
    """Return keys that finished training (non-blocking poll)."""
    return {
        k for k in active_keys
        if not global_predictor.is_training(k, "scalping")
    }


def _count_saved_models(models_dir: Path, symbols: list[str]) -> int:
    """Count how many scalping .pt files exist for the given symbols."""
    count = 0
    for sym in symbols:
        if (models_dir / f"{sym}_scalping_lstm.pt").exists():
            count += 1
    return count


def _reset_scalping_rl(mode: str = "paper") -> None:
    """
    Reset the scalping RL Q-table to defaults.
    Keeps n_updates so epsilon decay is preserved but clears
    Q-values and resets conf_thresh/risk_factor to defaults.
    """
    from ai.rl_agent import DATA_DIR, DEFAULT_CONF_THRESH, DEFAULT_RISK_FACTOR

    path = DATA_DIR / f"rl_qtable_scalping_{mode}.json"
    if not path.exists():
        logger.info(f"RL reset: {path.name} not found — agent will start fresh")
        return

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        n_updates = data.get("n_updates", 0)

        reset_data = {
            "q":           {},               # clear Q-table — relearn from scratch
            "conf_thresh": DEFAULT_CONF_THRESH,  # 0.55
            "risk_factor": DEFAULT_RISK_FACTOR,  # 1.00
            "last_state":  None,
            "last_action": None,
            "n_updates":   n_updates,        # preserve so epsilon stays at learned level
        }
        path.write_text(json.dumps(reset_data, indent=2), encoding="utf-8")
        logger.info(
            f"RL reset: scalping/{mode} → "
            f"conf_thresh={DEFAULT_CONF_THRESH} "
            f"risk_factor={DEFAULT_RISK_FACTOR} "
            f"(n_updates={n_updates} preserved)"
        )
    except Exception as exc:
        logger.error(f"RL reset failed: {exc}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Scalping-only LSTM retrainer")
    parser.add_argument("--dry-run",      action="store_true", help="Print plan without training")
    parser.add_argument("--no-rl-reset",  action="store_true", help="Skip RL Q-table reset after training")
    args = parser.parse_args()

    config_dir = ROOT / "config"
    models_dir = ROOT / "ai" / "models"

    logger.info("=" * 70)
    logger.info("AI-BOT-MT5  Scalping LSTM Retrainer  (2-year M5 data)")
    logger.info("=" * 70)

    symbols = _build_scalping_jobs(config_dir)
    if not symbols:
        logger.error("No enabled scalping symbols found in config/symbols.json")
        sys.exit(1)

    logger.info(f"Scalping symbols to retrain: {len(symbols)}")
    for s in symbols:
        logger.info(f"  {s}")

    # ── Date range: 2 years back from now ────────────────────────────────────
    date_to   = datetime.now(tz=timezone.utc)
    date_from = date_to - timedelta(days=365 * LOOKBACK_YEARS)
    logger.info(f"\nDate range: {date_from.date()} → {date_to.date()} ({LOOKBACK_YEARS} years)")

    if args.dry_run:
        logger.info("\n[DRY RUN] — no training will be performed")
        logger.info(f"Would fetch M5 data from {date_from.date()} to {date_to.date()}")
        logger.info(f"Would train {len(symbols)} scalping models")
        logger.info(f"MAX_CONCURRENT_TRAIN = {MAX_CONCURRENT_TRAIN}")
        return

    # ── Phase 1: Fetch all data upfront ──────────────────────────────────────
    logger.info("\nPhase 1 — Fetching 2-year M5 data via get_ohlcv_range()…")
    data_cache: dict[str, Optional[pd.DataFrame]] = {}

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        for symbol in symbols:
            df = client.get_ohlcv_range(symbol, "M5", date_from, date_to)
            data_cache[symbol] = df
            bar_count = len(df) if df is not None else 0
            status    = f"{bar_count:>9,d} bars" if df is not None else "   NO DATA"
            logger.info(f"  {symbol:25s} M5 → {status}")

    # ── Phase 2: Train all models ─────────────────────────────────────────────
    logger.info(f"\nPhase 2 — Training LSTMs (max {MAX_CONCURRENT_TRAIN} concurrent)…")

    total_submitted = 0
    skipped         = 0
    active_keys:    set[str] = set()

    for idx, symbol in enumerate(symbols, 1):
        df = data_cache.get(symbol)
        if df is None or df.empty:
            logger.warning(f"  [{idx:>2d}] {symbol}: no data — skipped")
            skipped += 1
            continue

        # Throttle: wait for a free concurrency slot
        while len(active_keys) >= MAX_CONCURRENT_TRAIN:
            done = _wait_batch(active_keys)
            active_keys -= done
            for k in done:
                logger.info(f"  ✓ finished {k}/scalping")
            if not done:
                time.sleep(5.0)

        if global_predictor.train_async(symbol, df, "scalping"):
            active_keys.add(symbol)
            total_submitted += 1
            logger.info(
                f"  [{idx:>2d}/{len(symbols)}] started  {symbol}/scalping "
                f"({len(df):,} bars)"
            )
        else:
            logger.warning(f"  [{idx:>2d}] {symbol}: already training — skipped")
            skipped += 1

    # Drain remaining jobs
    logger.info(f"\nWaiting for final {len(active_keys)} job(s)…")
    while active_keys:
        done = _wait_batch(active_keys)
        active_keys -= done
        for k in done:
            logger.info(f"  ✓ finished {k}/scalping")
        if active_keys:
            time.sleep(10.0)

    # ── Phase 3: Validate results ─────────────────────────────────────────────
    logger.info("\nPhase 3 — Validating saved models…")
    saved   = _count_saved_models(models_dir, symbols)
    success = saved / len(symbols) if symbols else 0.0
    logger.info(f"{saved}/{len(symbols)} models saved ({success:.0%} success rate)")

    # Print accuracy for each saved model
    for sym in sorted(symbols):
        meta_file = models_dir / f"{sym}_scalping_meta.json"
        if meta_file.exists():
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
                acc  = meta.get("accuracy", 0.0)
                marker = "✓" if acc >= 0.58 else "✗"
                logger.info(f"  {marker} {sym:25s}  acc={acc:.4f}")
            except Exception:
                logger.info(f"  ? {sym:25s}  (meta unreadable)")
        else:
            logger.info(f"  ✗ {sym:25s}  (model not saved — below accuracy gate)")

    # ── Phase 4: Reset RL agent ───────────────────────────────────────────────
    if not args.no_rl_reset:
        if success >= _MIN_SUCCESS_FRACTION:
            logger.info(
                f"\nPhase 4 — Resetting scalping RL Q-table "
                f"({success:.0%} models succeeded ≥ {_MIN_SUCCESS_FRACTION:.0%} threshold)…"
            )
            # Detect current account mode from account_store
            try:
                from engine.account_store import current_mode
                _mode = current_mode()
            except Exception:
                _mode = "paper"
            _reset_scalping_rl(mode=_mode)
        else:
            logger.warning(
                f"\nPhase 4 — RL reset SKIPPED "
                f"({success:.0%} success < {_MIN_SUCCESS_FRACTION:.0%} threshold) "
                "— too many models failed to justify resetting the agent."
            )
    else:
        logger.info("\nPhase 4 — RL reset skipped (--no-rl-reset flag)")

    logger.info("\n" + "=" * 70)
    logger.info(
        f"Scalping retrain complete — "
        f"{total_submitted} submitted  |  {skipped} skipped  |  "
        f"{saved} models saved"
    )
    logger.info("=" * 70)


if __name__ == "__main__":
    main()