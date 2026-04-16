"""
RL Bootstrap from Backtests
==============================
Seeds the Q-tables for all nine per-strategy RL agents by running the
backtester on 2 years of historical data with use_ai_filters=True, so
every trade the RL agent learns from carries a real LSTM confidence score.

Process:
  1. Reset all nine per-strategy Q-tables to clean defaults (idempotent)
  2. Connect to MT5, fetch 2-year OHLCV data per symbol/timeframe
  3. Run walk-forward backtest for each symbol × strategy with AI filters on
  4. Collect all BacktestTrade objects tagged with their strategy name
  5. Replay per strategy through its dedicated rl_agent.observe()
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
from ai.rl_agent import RLAgent, rl_manager as _rl_manager, _ALL_STRATEGIES

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 2 years of bars per trading type — synced with run_retrain.py
_BARS: dict[str, int] = {
    "scalping":     200_000,   # M5  ~2 years
    "day_trading":   50_000,   # H1  ~5.7 years
    "swing":         30_000,   # H4  ~13.7 years
}

_D1_BARS = 1_200   # ~3.5 years of daily bars (ema_trend_rider / weekly_breakout)

_TF: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

# Strategies that require a separate D1 dataframe
_NEEDS_D1: set[str] = {"ema_trend_rider", "weekly_breakout"}

# Rolling window for win-rate calculation (trades per strategy)
_WR_WINDOW = 20

_DATA_DIR = ROOT / "ai" / "data"

# Per-strategy Q-table paths (9 files)
_QTABLE_FILES: dict[str, Path] = {
    f"{strategy}_{trading_type}": _DATA_DIR / f"rl_qtable_{strategy}_{trading_type}_paper.json"
    for trading_type, strategies in _ALL_STRATEGIES.items()
    for strategy in strategies
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
    logger.info(f"Q-tables reset to clean defaults ({len(_QTABLE_FILES)} per-strategy files)")


def _build_extra_dfs(
    strategy: str,
    primary_df: pd.DataFrame,
    d1_df: Optional[pd.DataFrame],
) -> dict[str, pd.DataFrame]:
    """
    Build the extra_dfs dict for run_backtest().

    Since we run every strategy against the trading_type's standard primary TF
    (M5 / H1 / H4), we pass the primary df as a proxy for same-granularity
    extra TFs. For coarser TFs (D1) we use the separately fetched d1_df.
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
    logger.info("AI-BOT-MT5  RL Bootstrap  (per-strategy, 2-year backtest seeding)")
    logger.info("=" * 70)

    # Step 1: reset Q-tables and reload the in-memory rl_manager singleton so
    # the backtester (which uses rl_manager for signal gating) starts with
    # conf_thresh=0.55, not stale values from a previous bootstrap run.
    _reset_qtables()
    _rl_manager.switch_mode("paper")

    config_dir  = ROOT / "config"
    symbols_cfg: dict = json.loads((config_dir / "symbols.json").read_text(encoding="utf-8"))

    # Each entry: (exit_time_str, trading_type, strategy_name, pnl_pct, conf_score, entry_price, sl_price)
    collected: list[tuple[str, str, str, float, float, float, float]] = []

    # ── Phase 1: fetch data and run backtests ─────────────────────────────
    logger.info("\nPhase 1 — Fetching 2-year OHLCV data and running backtests …")

    with MT5Client() as client:
        if not client.connect():
            logger.error("MT5 connection failed — check .env credentials")
            sys.exit(1)

        for trading_type, strategies in _ALL_STRATEGIES.items():
            tf             = _TF[trading_type]
            bars           = _BARS[trading_type]
            needs_d1       = any(s in _NEEDS_D1 for s in strategies)
            active_symbols = [
                s["symbol"] for s in symbols_cfg.get(trading_type, [])
                if s.get("enabled", True)
            ]

            logger.info(f"\n  [{trading_type.upper()}] fetching {tf} data (2yr ≈ {bars:,} bars) …")
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
                            strategy,
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

    # Sort all trades chronologically
    collected.sort(key=lambda x: x[0])

    # ── Phase 2: replay per strategy through its dedicated RL agent ───────
    logger.info("\nPhase 2 — Replaying trades per strategy through RL agent observe() …")

    # Clamp ranges prevent an extreme backtest from writing a panic state
    _CLAMP_CONF_FLOOR = {"scalping": 0.54, "day_trading": 0.54, "swing": 0.52}
    _CLAMP_CONF_CEIL  = {"scalping": 0.68, "day_trading": 0.72, "swing": 0.70}
    _CLAMP_RF_FLOOR   = {"scalping": 0.70, "day_trading": 0.70, "swing": 0.70}
    _CLAMP_RF_CEIL    = {"scalping": 1.20, "day_trading": 1.30, "swing": 1.40}

    for trading_type, strategies in _ALL_STRATEGIES.items():
        for strategy in strategies:
            strat_trades = [t for t in collected if t[1] == trading_type and t[2] == strategy]
            if not strat_trades:
                logger.warning(f"  {strategy} ({trading_type}): no trades — Q-table stays at default")
                continue

            agent   = RLAgent(trading_type=trading_type, mode="paper", strategy_name=strategy)
            window: deque[bool] = deque(maxlen=_WR_WINDOW)
            equity  = 10_000.0
            peak_eq = equity
            wins    = 0

            for _, _, _, pnl_pct, conf_score, entry, sl in strat_trades:
                win = pnl_pct > 0
                if win:
                    wins += 1
                window.append(win)
                equity   *= 1.0 + pnl_pct / 100.0
                peak_eq   = max(peak_eq, equity)
                dd_pct    = max(0.0, (peak_eq - equity) / peak_eq * 100.0)
                wr        = sum(window) / len(window) if window else 0.5
                # Deflate backtest confidence 5% to approximate realistic live distribution
                _conf_deflated = conf_score * 0.95 if conf_score > 0.0 else 0.55
                avg_conf  = max(0.50, _conf_deflated)
                # Apply spread penalty so RL agent learns spread-adjusted outcomes
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

            # Sanity clamp on persisted Q-table
            _qp = _QTABLE_FILES[f"{strategy}_{trading_type}"]
            try:
                _qt = json.loads(_qp.read_text(encoding="utf-8"))
                _qt["conf_thresh"] = max(
                    _CLAMP_CONF_FLOOR[trading_type],
                    min(_CLAMP_CONF_CEIL[trading_type], _qt.get("conf_thresh", 0.55))
                )
                _qt["risk_factor"] = max(
                    _CLAMP_RF_FLOOR[trading_type],
                    min(_CLAMP_RF_CEIL[trading_type], _qt.get("risk_factor", 1.0))
                )
                _qp.write_text(json.dumps(_qt), encoding="utf-8")
            except Exception as _ce:
                logger.warning(f"Bootstrap clamp failed for {strategy}/{trading_type}: {_ce}")

            wr_final = wins / len(strat_trades)
            logger.info(
                f"  {strategy:20s} ({trading_type:12s}): {len(strat_trades):>5,d} trades  "
                f"WR={wr_final:.1%}  "
                f"conf_thresh={agent.confidence_threshold:.2f}  "
                f"risk_factor={agent.risk_factor:.2f}"
            )

    logger.info("\n" + "=" * 70)
    logger.info("RL bootstrap complete.  Q-tables seeded with real LSTM-scored 2-year backtest data.")
    logger.info("Next step: enable confidence_filter_enabled=true in config/app.json")
    logger.info("=" * 70)


if __name__ == "__main__":
    main()

