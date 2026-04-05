"""
RL Bootstrap from Backtests
==============================
Seeds the Q-tables for all three trading types by running the backtester
on 1 year of historical data with use_ai_filters=True, so every trade
the RL agent learns from carries a real LSTM confidence score.

Process:
  1. Reset all three Q-tables to clean defaults (idempotent guarantee —
     run this script as many times as needed; result is always consistent)
  2. Connect to MT5, fetch 1-year OHLCV data per symbol/timeframe
  3. Run walk-forward backtest for each symbol × strategy with AI filters on
  4. Collect all BacktestTrade objects, sorted chronologically
  5. Replay through rl_agent.observe() per trading_type
  6. Force-save Q-tables to disk

Must be run AFTER:
  • run_retrain.py   — LSTM models must be trained for accurate confidence scores
  • run_optimizer.py — optimised params improve backtest signal quality

Usage (from workspace root):
    python ai/run_rl_bootstrap.py
"""
from __future__ import annotations

import json
import sys
from collections import deque
from pathlib import Path
from typing import Optional

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

from engine.backtester import run_backtest, BT_EXTRA_TIMEFRAMES
from engine.mt5_client import MT5Client
from ai.rl_agent import RLAgent, rl_manager as _rl_manager

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 1 year of bars per trading type (enough for Q-table coverage; faster than optimizer's 2 years)
_BARS: dict[str, int] = {
    "scalping":     250_000,   # M5  ~2.38 years
    "day_trading":   50_000,   # H1  ~5.71 years
    "swing":         30_000,   # H4  ~13.7 years
}

_D1_BARS = 1_200   # 3.5 year of daily bars (for ema_trend_rider / weekly_breakout)

_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

_STRATEGIES: dict[str, list[str]] = {
    "scalping":    ["ema_scalp", "bb_squeeze", "vwap_reversion"],
    "day_trading": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "swing":       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
}

# Strategies that require a separate D1 dataframe
_NEEDS_D1: set[str] = {"ema_trend_rider", "weekly_breakout"}

# Rolling window for win-rate calculation (trades per trading_type)
_WR_WINDOW = 20

_DATA_DIR = ROOT / "ai" / "data"
_QTABLE_FILES = {
    "scalping":    _DATA_DIR / "rl_qtable_scalping_paper.json",
    "day_trading": _DATA_DIR / "rl_qtable_day_trading_paper.json",
    "swing":       _DATA_DIR / "rl_qtable_swing_paper.json",
}
_EMPTY_QTABLE = {
    "q": {}, "conf_thresh": 0.55, "risk_factor": 1.0,
    "last_state": None, "last_action": None, "n_updates": 0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_qtables() -> None:
    """Write clean default Q-tables so every bootstrap run starts from the same state."""
    for path in _QTABLE_FILES.values():
        path.write_text(json.dumps(_EMPTY_QTABLE), encoding="utf-8")
    logger.info("Q-tables reset to clean defaults (conf_thresh=0.55, risk_factor=1.0)")


def _build_extra_dfs(
    strategy: str,
    primary_df: pd.DataFrame,
    d1_df: Optional[pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """
    Build the extra_dfs dict for run_backtest().

    Since we run every strategy against the trading_type's standard primary TF
    (M5 / H1 / H4), we pass the primary df as a proxy for same-granularity
    extra TFs (e.g. df_m5 for ema_scalp, df_h1 for macd_ema_trend).
    For coarser TFs (D1) we use the separately fetched d1_df.
    """
    extra: dict[str, pd.DataFrame] = {}
    for kwarg, tf in BT_EXTRA_TIMEFRAMES.get(strategy, {}).items():
        if tf == "D1" or kwarg == "df_daily":
            if d1_df is not None:
                extra[kwarg] = d1_df
        else:
            extra[kwarg] = primary_df   # same-granularity proxy
    return extra

_SPREAD_PNL_PENALTY: dict[str, float] = {
    "scalping":    0.04,   # ~2 pip round-trip on tight scalp SL = 4% pnl penalty
    "day_trading": 0.015,  # ~2 pip on wider H1 SL = 1.5% penalty
    "swing":       0.010,  # ~2 pip on wide H4 SL = 0.5% penalty
}
# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("=" * 70)
    logger.info("AI-BOT-MT5  RL Bootstrap  (backtest-based seeding)")
    logger.info("=" * 70)

    # Step 1: reset Q-tables and reload the in-memory rl_manager singleton so
    # the backtester (which uses rl_manager for signal gating) starts with
    # conf_thresh=0.55, not stale values from a previous bootstrap run.
    _reset_qtables()
    _rl_manager.switch_mode("paper")

    config_dir  = ROOT / "config"
    symbols_cfg: dict = json.loads((config_dir / "symbols.json").read_text(encoding="utf-8"))

    # Each entry: (exit_time_str, trading_type, pnl_pct, conf_score, entry_price, sl_price)
    collected: list[tuple[str, str, float, float, float, float]] = []

    # ── Phase 1: fetch data and run backtests ─────────────────────────────
    logger.info("\nPhase 1 — Fetching OHLCV data and running backtests …")

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        for trading_type, strategies in _STRATEGIES.items():
            tf             = _TF[trading_type]
            bars           = _BARS[trading_type]
            needs_d1       = any(s in _NEEDS_D1 for s in strategies)
            active_symbols = [
                s["symbol"] for s in symbols_cfg.get(trading_type, [])
                if s.get("enabled", True)
            ]

            logger.info(f"\n  [{trading_type.upper()}] fetching {tf} data …")
            primary_cache: dict[str, Optional[pd.DataFrame]] = {}
            d1_cache:      dict[str, Optional[pd.DataFrame]] = {}

            for symbol in active_symbols:
                df = client.get_ohlcv(symbol, tf, count=bars)
                primary_cache[symbol] = df
                if df is not None:
                    logger.info(f"    {symbol:20s} {tf:3s} → {len(df):>7,d} bars")
                else:
                    logger.warning(f"    {symbol:20s} {tf:3s} → NO DATA")

                if needs_d1:
                    d1_cache[symbol] = client.get_ohlcv(symbol, "D1", count=_D1_BARS)

            logger.info(f"  [{trading_type.upper()}] running backtests …")
            for symbol in active_symbols:
                primary_df = primary_cache.get(symbol)
                if primary_df is None:
                    continue
                d1_df = d1_cache.get(symbol) if needs_d1 else None

                for strategy in strategies:
                    extra_dfs = _build_extra_dfs(strategy, primary_df, d1_df)
                    try:
                        result = run_backtest(
                            strategy_name  = strategy,
                            symbol         = symbol,
                            df             = primary_df,
                            trading_type   = trading_type,
                            use_ai_filters = True,
                            extra_dfs      = extra_dfs,
                        )
                    except Exception as exc:
                        logger.warning(f"    Backtest failed {strategy}/{symbol}: {exc}")
                        continue

                    if result is None or result.total_trades == 0:
                        continue

                    for t in result.trades:
                        collected.append((
                            t.exit_time,
                            trading_type,
                            t.pnl_pct,
                            t.conf_score,
                            t.entry_price,
                            t.sl_price,
                        ))

                    logger.info(
                        f"    {strategy}/{symbol}: {result.total_trades} trades  "
                        f"WR={result.win_rate:.1%}  avg_conf={result.avg_confidence:.2f}"
                    )

    total = len(collected)
    logger.info(f"\nTotal backtest trades collected: {total:,}")

    if total == 0:
        logger.error(
            "No trades collected — cannot seed Q-tables.\n"
            "Ensure MT5 is connected and LSTM models are trained (run_retrain.py first)."
        )
        sys.exit(1)

    # Sort all trades chronologically across all symbols/strategies
    collected.sort(key=lambda x: x[0])

    # ── Phase 2: replay per trading_type through RL agent ─────────────────
    logger.info("\nPhase 2 — Replaying trades through RL agent observe() …")

    for trading_type in _STRATEGIES:
        type_trades = [t for t in collected if t[1] == trading_type]
        if not type_trades:
            logger.warning(f"  {trading_type}: no trades — Q-table stays at default")
            continue

        agent   = RLAgent(trading_type=trading_type, mode="paper")
        window: deque[bool] = deque(maxlen=_WR_WINDOW)
        equity  = 10_000.0
        peak_eq = equity
        wins    = 0

        for exit_time, _, pnl_pct, conf_score, entry, sl in type_trades:
            win = pnl_pct > 0
            if win:
                wins += 1
            window.append(win)
            equity   *= 1.0 + pnl_pct / 100.0
            peak_eq   = max(peak_eq, equity)
            dd_pct    = max(0.0, (peak_eq - equity) / peak_eq * 100.0)
            wr        = sum(window) / len(window) if window else 0.5
            avg_conf  = conf_score if conf_score > 0.0 else 0.55
             # Apply spread penalty so RL agent learns from spread-adjusted outcomes
            _penalty = _SPREAD_PNL_PENALTY.get(trading_type, 0.01)
            _adjusted_pnl = pnl_pct - (_penalty * 100.0)
            reward    = _adjusted_pnl / 100.0
            vol_pct   = abs(entry - sl) / entry * 100.0 if entry > 0 else 0.5

            agent.observe(
                win_rate     = wr,
                avg_conf     = avg_conf,
                reward       = reward,
                drawdown_pct = dd_pct,
                vol_pct      = vol_pct,
            )

        agent.shutdown()
        wr_final = wins / len(type_trades)
        logger.info(
            f"  {trading_type:12s}: {len(type_trades):>5,d} trades  "
            f"WR={wr_final:.1%}  "
            f"conf_thresh={agent.confidence_threshold:.2f}  "
            f"risk_factor={agent.risk_factor:.2f}"
        )

    logger.info("\n" + "=" * 70)
    logger.info("RL bootstrap complete.  Q-tables are seeded with real LSTM-scored backtest data.")
    logger.info("Next step: enable confidence_filter_enabled=true in config/app.json")
    logger.info("=" * 70)

if __name__ == "__main__":
    main()
