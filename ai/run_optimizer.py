"""
Standalone Parameter Optimizer
================================
Connects to MT5, fetches 2 years of OHLCV data for every active
symbol × strategy pair, then runs the walk-forward grid-search
optimizer (MAX_CONCURRENT_OPT overridden to 12 for standalone runs) and writes results to
config/optimized_params.json.

Optimized params take effect on the next strategy scan — no restart needed.

Usage (from workspace root):
    python -m ai.run_optimizer
    python ai/run_optimizer.py
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
import ai.param_optimizer as _opt_module
from ai.param_optimizer import ParamOptimizer, MAX_CONCURRENT_OPT as _APP_MAX_CONCURRENT

# Override concurrency for standalone runs — app is not up so no MT5 contention.
# The live app uses MAX_CONCURRENT_OPT=2 to avoid CPU starvation during trading;
# here all data is already in memory so we can saturate all 12 logical processors.
MAX_CONCURRENT_OPT = 12
_opt_module.MAX_CONCURRENT_OPT = MAX_CONCURRENT_OPT

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bars to fetch per trading type (covers 2+ years with margin).
# Forex/crypto trade ~22 h/day; stocks/indices fewer — MT5 returns what it has.
_BARS: dict[str, int] = {
    "scalping":     99_000,   # M5:  ~1 year (MT5 per-request limit is ~99k)
    "day_trading":  17_000,   # H1:  ~2 years
    "swing":         5_000,   # H4:  ~2 years
}

_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

# Strategies per trading type (must match strategies.json active lists)
_STRATEGIES: dict[str, list[str]] = {
    "scalping":    ["ema_scalp", "bb_squeeze", "vwap_reversion"],
    "day_trading": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "swing":       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_jobs(config_dir: Path) -> list[tuple[str, str, str]]:
    """Return sorted list of (strategy, symbol, trading_type) tuples."""
    symbols_cfg = json.loads((config_dir / "symbols.json").read_text(encoding="utf-8"))
    jobs: list[tuple[str, str, str]] = []
    for trading_type, strategies in _STRATEGIES.items():
        active_symbols = [
            s["symbol"]
            for s in symbols_cfg.get(trading_type, [])
            if s.get("enabled", True)
        ]
        for symbol in active_symbols:
            for strategy in strategies:
                jobs.append((strategy, symbol, trading_type))
    return jobs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config_dir = ROOT / "config"
    optimizer  = ParamOptimizer()

    logger.info("=" * 70)
    logger.info("AI-BOT-MT5  Parameter Optimizer  (standalone run)")
    logger.info("=" * 70)

    jobs = _build_jobs(config_dir)
    logger.info(f"Total jobs queued: {len(jobs)}")

    # ── Phase 1: fetch all historical data upfront ───────────────────────────
    logger.info("\nPhase 1 — Fetching historical data …")
    data_cache: dict[tuple[str, str], Optional[pd.DataFrame]] = {}

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        seen: set[tuple[str, str]] = set()
        for _strategy, symbol, trading_type in jobs:
            key = (symbol, trading_type)
            if key in seen:
                continue
            seen.add(key)

            tf   = _TF[trading_type]
            bars = _BARS[trading_type]
            df   = client.get_ohlcv(symbol, tf, count=bars)
            data_cache[key] = df

            if df is not None:
                logger.info(f"  {symbol:20s} {tf:3s} → {len(df):>8,d} bars")
            else:
                logger.warning(f"  {symbol:20s} {tf:3s} → NO DATA (symbol not in Market Watch?)")

    # ── Phase 2: submit jobs, respecting MAX_CONCURRENT_OPT ─────────────────
    logger.info(f"\nPhase 2 — Optimizing (max {MAX_CONCURRENT_OPT} concurrent jobs) …")

    total     = len(jobs)
    submitted = 0
    skipped   = 0

    for idx, (strategy, symbol, trading_type) in enumerate(jobs, 1):
        df = data_cache.get((symbol, trading_type))
        if df is None:
            logger.warning(f"  [{idx:>3d}/{total}] {strategy}/{symbol}: no data — skipped")
            skipped += 1
            continue

        # Block until a concurrency slot is free
        while not optimizer.optimize_async(strategy, symbol, df, trading_type):
            time.sleep(2.0)

        submitted += 1
        logger.info(
            f"  [{submitted:>3d}/{total}] submitted  {strategy}/{symbol}  ({trading_type})"
        )

    # ── Phase 3: wait for all running jobs to finish ─────────────────────────
    logger.info(f"\nPhase 3 — Waiting for {submitted} submitted job(s) to complete …")
    last_count = -1
    while True:
        running = len(optimizer._running)
        if running == 0:
            break
        if running != last_count:
            logger.info(f"  … {running} job(s) still running")
            last_count = running
        time.sleep(5.0)

    # ── Phase 4: summary ─────────────────────────────────────────────────────
    status  = optimizer.status()
    success = sum(1 for v in status.values() if v.get("best_score", 0) > 0)
    failed  = sum(
        1 for v in status.values()
        if v.get("best_score", 0) == 0 and not v.get("running", False)
    )

    logger.info("\n" + "=" * 70)
    logger.info(
        f"Optimization complete — "
        f"{success} succeeded  |  {failed} failed/no-signal  |  {skipped} skipped"
    )
    logger.info(f"Results written to: {ROOT / 'config' / 'optimized_params.json'}")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
