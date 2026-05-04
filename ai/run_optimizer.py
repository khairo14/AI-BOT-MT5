"""
Standalone Parameter Optimizer  (multiprocessing edition)
===========================================================
Connects to MT5, fetches 2 years of OHLCV data for every active
symbol × strategy pair, then runs the walk-forward grid-search
optimizer using one OS process per job (true CPU parallelism —
bypasses the Python GIL) and writes results to
config/optimized_params.json.

Jobs already completed with 2-year data are automatically skipped,
so it is safe to stop and resume this script at any time.

Usage (from workspace root):
    python -m ai.run_optimizer
    python ai/run_optimizer.py
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

from engine.mt5_client import MT5Client
from ai.param_optimizer import ParamOptimizer

# Number of parallel OS processes.  Each gets its own Python interpreter
# and GIL, so all MAX_WORKERS cores run simultaneously.
MAX_WORKERS = 12

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_BARS: dict[str, int] = {
    "scalping":     250_000,   # M5:  ~2.38 years
    "day_trading":  50_000,   # H1:  ~5.71 years
    "swing":         30_000,   # H4:  ~13.7 years
}

_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

_STRATEGIES: dict[str, list[str]] = {
    "scalping":    ["ema_scalp", "bb_squeeze", "vwap_reversion","stoch_rsi_pullback"],
    "day_trading": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "swing":       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
}

# Jobs with bars_used >= mode-specific threshold are treated as already done and skipped.
# Set to ~80% of requested bars to handle broker data limitations
_DONE_BARS_THRESHOLD: dict[str, int] = {
    "scalping":     250_000,   # 80% of 250k — handles symbols that only return ~200-250k bars
    "day_trading":  50_000,    # 80% of 50k  — day trades need at least 40k H1 bars (~4.5 years)
    "swing":        30_000,    # ~83% of 30k — swing trades need at least 25k H4 bars (~11 years)
}


# ---------------------------------------------------------------------------
# Worker  (must be a module-level function so it can be pickled by spawn)
# ---------------------------------------------------------------------------

def _run_job(args: tuple) -> tuple:
    """Runs in a child OS process — full CPU core, own GIL.

    Returns (strategy_name, symbol, trading_type, best_params, score,
             n_signals, regime_params, bars_used).
    """
    strategy_name, symbol, df, trading_type = args
    # Fresh import inside the child process ensures no shared state.
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).parent.parent))
    from ai.param_optimizer import ParamOptimizer
    opt = ParamOptimizer()
    best_params, score, n, regime_params = opt._run_backtest(
        strategy_name, symbol, df, trading_type
    )
    return strategy_name, symbol, trading_type, best_params, score, n, regime_params, len(df)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_jobs(config_dir: Path) -> list[tuple[str, str, str]]:
    """Return (strategy, symbol, trading_type) jobs from:
    1. config/scanner.json  — dynamic live symbol list per mode (primary)
    2. config/symbols.json  — static baseline (merged as fallback/supplement)
    Union ensures optimizer covers every symbol the bot may actually trade.
    """
    symbols_cfg = json.loads((config_dir / "symbols.json").read_text(encoding="utf-8"))
    jobs: list[tuple[str, str, str]] = []
    for trading_type, strategies in _STRATEGIES.items():
        seen_syms: set[str] = set()
        active_symbols: list[str] = []

        # Primary: scanner.json (the live dynamic list)
        scanner_path = config_dir / "scanner.json"
        if scanner_path.exists():
            try:
                scanner_cfg = json.loads(scanner_path.read_text(encoding="utf-8"))
                for sym in scanner_cfg.get(trading_type, {}).get("symbols", []):
                    if sym and sym not in seen_syms:
                        active_symbols.append(sym)
                        seen_syms.add(sym)
            except Exception as exc:
                logger.warning(f"Could not read scanner.json for symbol list: {exc}")

        # Supplement: symbols.json static baseline
        for s in symbols_cfg.get(trading_type, []):
            sym = s.get("symbol")
            if sym and s.get("enabled", True) and sym not in seen_syms:
                active_symbols.append(sym)
                seen_syms.add(sym)

        for symbol in active_symbols:
            for strategy in strategies:
                jobs.append((strategy, symbol, trading_type))
    return jobs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config_dir = ROOT / "config"
    optimizer  = ParamOptimizer()   # main-process instance — handles all file I/O

    logger.info("=" * 70)
    logger.info("AI-BOT-MT5  Parameter Optimizer  (multiprocessing — true CPU parallelism)")
    logger.info(f"Workers: {MAX_WORKERS} OS processes")
    logger.info("=" * 70)

    jobs = _build_jobs(config_dir)
    logger.info(f"Total jobs: {len(jobs)}")

    # ── Load existing 2yr results so we can skip them ────────────────────────
    status_file = ROOT / "ai" / "data" / "optimizer_status.json"
    existing: dict = {}
    if status_file.exists():
        try:
            existing = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Mode-aware threshold: check bars_used against the correct threshold for each trading_type
    already_done: set[str] = set()
    for k, v in existing.items():
        ttype = v.get("trading_type", "scalping")  # fallback to scalping if missing
        threshold = _DONE_BARS_THRESHOLD.get(ttype, 30000)
        if v.get("bars_used", 0) >= threshold:
            already_done.add(k)
    logger.info(f"Already completed: {len(already_done)} jobs — skipping")

    # ── Phase 1: fetch all historical data upfront ───────────────────────────
    logger.info("\nPhase 1 — Fetching historical data …")
    data_cache: dict[tuple[str, str], Optional[pd.DataFrame]] = {}

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        seen: set[tuple[str, str]] = set()
        for _strategy, symbol, trading_type in jobs:
            cache_key = (symbol, trading_type)
            if cache_key in seen:
                continue
            seen.add(cache_key)
            tf  = _TF[trading_type]
            bar = _BARS[trading_type]
            df  = client.get_ohlcv(symbol, tf, count=bar)
            data_cache[cache_key] = df
            if df is not None:
                logger.info(f"  {symbol:20s} {tf:3s} → {len(df):>8,d} bars")
            else:
                logger.warning(f"  {symbol:20s} {tf:3s} → NO DATA (symbol not in Market Watch?)")

    # ── Phase 2: build pending job list (skip done + no-data) ────────────────
    pending: list[tuple] = []
    skipped_done   = 0
    skipped_nodata = 0

    for strategy, symbol, trading_type in jobs:
        key = f"{strategy}__{symbol}"
        if key in already_done:
            skipped_done += 1
            continue
        df = data_cache.get((symbol, trading_type))
        if df is None:
            skipped_nodata += 1
            continue
        pending.append((strategy, symbol, df, trading_type))

    total = len(pending)
    logger.info(
        f"\nPhase 2 — {total} jobs to run  "
        f"({skipped_done} already-done skipped, {skipped_nodata} no-data skipped)"
    )
    logger.info(f"Dispatching to {MAX_WORKERS} parallel processes …\n")

    success = 0
    failed  = 0

    # ── Phase 3: multiprocessing — each job owns a full CPU core ─────────────
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_map = {
            executor.submit(_run_job, job): f"{job[0]}__{job[1]}"
            for job in pending
        }
        for idx, future in enumerate(as_completed(future_map), 1):
            try:
                strat, sym, ttype, best_params, score, n, regime_params, bars = future.result()
                key = f"{strat}__{sym}"
                now = datetime.now(tz=timezone.utc).isoformat()

                if best_params is not None and score > 0.0:
                    optimizer._save_params(strat, sym, best_params, regime_params)
                    with optimizer._lock:
                        optimizer._status[key] = {
                            "strategy":          strat,
                            "symbol":            sym,
                            "trading_type":      ttype,
                            "best_score":        round(score, 4),
                            "n_signals":         n,
                            "best_params":       best_params,
                            "regime_params":     regime_params,
                            "last_optimized_at": now,
                            "bars_used":         bars,
                        }
                    optimizer._save_status()
                    success += 1
                    logger.info(
                        f"  [{idx:>3d}/{total}] ✓  {strat}/{sym}"
                        f"  score={score:.4f}  n={n}"
                    )
                else:
                    with optimizer._lock:
                        optimizer._status[key] = {
                            "strategy":          strat,
                            "symbol":            sym,
                            "trading_type":      ttype,
                            "last_attempted_at": now,
                            "best_score":        0.0,
                            "bars_used":         bars,
                        }
                    optimizer._save_status()
                    failed += 1
                    logger.warning(
                        f"  [{idx:>3d}/{total}] ✗  {strat}/{sym}  no valid combo"
                    )

            except Exception as exc:
                failed += 1
                logger.exception(
                    f"  [{idx:>3d}/{total}] ERROR {future_map[future]}: {exc}"
                )

    # ── Summary ──────────────────────────────────────────────────────────────
    logger.info("\n" + "=" * 70)
    logger.info(
        f"Optimization complete — "
        f"{success} succeeded  |  {failed} failed/no-signal  |  "
        f"{skipped_done} already-done skipped  |  {skipped_nodata} no-data skipped"
    )
    logger.info(f"Results written to: {ROOT / 'config' / 'optimized_params.json'}")
    logger.info("=" * 70)
    
    # Broadcast optimizer_complete notification
    try:
        import requests
        best_score_val = max(
            (v.get("best_score", 0.0) for v in optimizer._status.values()),
            default=0.0,
        )
        # Trigger WebSocket broadcast via internal API endpoint (if API is running)
        requests.post(
            "http://127.0.0.1:8000/internal/optimizer-complete",
            json={"completed": success, "best_score": best_score_val},
            timeout=1.0,
        )
    except Exception:
        pass  # API may not be running — notification is optional


if __name__ == "__main__":
    main()
