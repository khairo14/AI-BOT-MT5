"""
Strategy Parameter Optimizer

Two-phase approach:
  Phase 1 — Backtest grid search over historical OHLCV bars.
             Tests parameter combinations walk-forward, scores by win_rate × avg_rr.

  Phase 2 — Live refinement.
             Reads trade_memory.jsonl outcomes for a (strategy, symbol) pair.
             If win_rate drops below threshold (>=20 trades), triggers re-backtest
             with more bars to find updated best params.

Optimized params are written to: config/optimized_params.json
Format:
  {
    "ema_scalp": {
      "EURUSD":    {"ema_fast": 10, "rr": 2.5},
      "__global__": {"ema_fast": 8}
    }
  }

StrategyRunner._strategy_params() reads this file and merges over defaults,
so optimizations take effect on the next strategy run without restart.
"""

from __future__ import annotations

import collections
import itertools
import json
from engine.notification_manager import notification_manager
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd
from loguru import logger

CONFIG_DIR = Path(__file__).parent.parent / "config"
OPT_FILE   = CONFIG_DIR / "optimized_params.json"

MIN_TRADES_FOR_REFINEMENT = 20   # closed trades before live refinement kicks in
# Discard combos with fewer signals than these thresholds:
# Scalping (M5, 250k bars ~2.4yr) needs lower threshold due to extended history
# Day/swing need higher threshold for statistical significance
MIN_BACKTEST_SIGNALS: dict[str, int] = {
    "scalping":     7,    # 250k M5 bars over 2+ years → ~3 signals/year minimum
    "day_trading":  10,   # 50k H1 bars over 5+ years → ~2 signals/year minimum  
    "swing":        8,    # 30k H4 bars over 13+ years → ~0.6 signals/year minimum
}
MAX_GRID_COMBOS           = 64   # cap to keep backtest fast
BACKTEST_COOLDOWN_HOURS   = 24   # min hours between automatic re-backtests
LIVE_REFINE_WIN_THRESH    = 0.45 # re-optimize when win_rate drops below this
MAX_CONCURRENT_OPT        = 3    # max simultaneous optimizer jobs (prevents CPU starvation / MT5 disconnect)

# Spread cost in R-units (round-trip bid/ask spread + typical slippage per mode).
# This is subtracted from each simulated trade so unrealistic tight-spread combos
# are penalised during grid search.
SPREAD_COST_R: dict[str, float] = {
    "scalping":    0.20,   # tight stops → spread ~15% of 1R
    "day_trading": 0.08,   # spread ~5% of 1R
    "swing":       0.03,   # spread ~2% of 1R for wide swing targets
}

# Per-symbol overrides: these assets have much wider spreads than typical forex.
# Checked first in _run_backtest(); falls back to SPREAD_COST_R[trading_type] if symbol absent.
SPREAD_COST_R_SYMBOL: dict[str, float] = {
    # Crypto — very wide spreads on XM Standard
    "BTCUSD":     0.40,  "ETHUSD":     0.40,
    "XRPUSD":     0.35,  "SOLUSD":     0.35,
    # Gold / Silver
    "XAUUSD":     0.15,  "GOLD":       0.15,
    "SILVER":     0.18,
    # Oil
    "USOIL":      0.15,  "UKOIL":      0.15,
    "OILCash":    0.15,  "BRENTCash":  0.15,
    "NGASCash":   0.20,
    # US Indices
    "US30Cash":   0.10,  "US100Cash":  0.10,  "US500Cash":  0.10,
    # EU Indices
    "GER40Cash":  0.12,  "UK100Cash":  0.12,  "FRA40Cash":  0.12,
    # Stocks — widest spreads
    "Tesla":      0.35,  "Nvidia":     0.35,  "Apple":      0.30,
    "Microsoft":  0.30,  "Amazon":     0.35,  "Google":     0.35,
    "Facebook":   0.35,  "Netflix":    0.35,  "AdvMicroDev":0.35,
}

# Walk-forward step (every Nth bar) and max hold per mode
_BACKTEST_CONFIG = {
    # warmup=60: ema_scalp requires ema_bias_period(50) M5 bars for bias filter.
    # step=10: check every 10 M5 bars (50 min) — dense enough for statistics,
    # required to keep backtest runtime tractable at 99K-bar datasets.
    "scalping":    {"step": 10, "max_hold": 50,  "warmup": 60},
    # warmup=210: macd_ema_trend/rsi_divergence require ema_bias(200)+5 H1 bars
    # before they activate. Starting below that just logs noise and wastes steps.
    "day_trading": {"step": 5,  "max_hold": 100, "warmup": 210},
    "swing":       {"step": 10, "max_hold": 200, "warmup": 200},
}

# Fixed history window passed to each strategy at every backtest step.
# Replaces the old growing-slice approach (O(n²)) with a constant-width window (O(n)).
# Safety guarantee: slice always ends at bar i (no future data); we only cut old history
# from the front, which cannot introduce lookahead. Window ≥ 3× max indicator period
# per trading type so all EMA/RSI/MACD values are fully converged.
_BT_WINDOW: dict[str, int] = {
    "scalping":    200,   # max param period = ema_bias(50)  → 4× coverage
    "day_trading": 500,   # max default period = ema_bias(200) → 2.5× coverage
    "swing":       400,   # max param period = lookback(100)  → 4× coverage
}

# Per-strategy overrides for bt_window and step.
# vwap_reversion is the critical case: its calculate() runs groupby("date") +
# expanding().std() on every bar of the slice — an O(n²) operation that causes
# the optimizer to hang when bt_window=200 and the dataset is 250k M5 bars.
# Reducing bt_window to one trading session (100 bars ≈ ~1 day on M1/M5)
# keeps the VWAP computation bounded to a single session which is also semantically
# correct (VWAP resets daily anyway). The larger step reduces total bar evaluations.
_BT_WINDOW_STRATEGY: dict[str, int] = {
    "vwap_reversion": 100,   # 100 M5 bars = ~8.3 hours — one trading session
}
_BT_STEP_STRATEGY: dict[str, int] = {
    "vwap_reversion": 20,    # check every 20 bars (100 min) instead of 10 (50 min)
}

# ── Parameter grids ──────────────────────────────────────────────────────────
# Only the most impactful parameters per strategy (keep total combos ≤ MAX_GRID_COMBOS)
PARAM_GRIDS: dict[str, dict[str, list]] = {
    "ema_scalp": {
        "ema_fast":        [5, 8, 10, 13],
        "ema_slow":        [18, 21, 26, 34],
        "rsi_min":         [40, 45, 50, 52],
        "rsi_max":         [60, 65, 70],
        "atr_sl_mult":     [1.0, 1.5, 2.0, 2.5],
        "adx_min":         [15, 20, 25],
        "ema_sep_mult":    [0.05, 0.10, 0.20],
        "max_spread_pips": [3.0, 4.0, 5.0],  # tuned per symbol (tight for majors, wide for exotics)
        "rr":              [1.5, 2.0, 2.5, 3.0],
    },
    "bb_squeeze": {
        "bb_period":          [15, 20, 25],
        "bb_std":             [1.5, 2.0, 2.5],
        "min_squeeze_bars":   [3, 5, 7],
        "max_squeeze_bars":   [20, 0],      # new: 0 = disabled
        "sl_atr_mult":        [1.0, 1.5, 2.0],
        "tp_atr_mult":        [2.0, 2.5, 3.0],
        "adx_min":            [15, 20, 25],  # new
        "ema_trend_period":   [30, 50, 70],
        "max_spread_pips":    [3.0, 4.0, 5.0],
    },
    "vwap_reversion": {
        "sigma_entry":       [1.0, 1.2, 1.5, 2.0],
        "sigma_sl":          [2.0, 2.5, 3.0],
        "rsi_period":        [7, 9, 14],
        "rsi_oversold":      [28, 32, 35],
        "rsi_overbought":    [65, 68, 72],
        "min_rr_to_vwap":    [0.4, 0.6, 0.8, 1.0],
        "vwap_std_window":   [15, 20, 30],
        "atr_vol_filter":    [0.8, 1.0, 1.5, 2.0],   # new: adaptive
        "atr_period":        [10, 14, 20],             # new
    },
    "stoch_rsi_pullback": {
        "ema_fast":             [13, 21, 34],
        "ema_mid":              [40, 50, 60],
        "stochrsi_oversold":    [15, 20, 25],
        "stochrsi_overbought":  [75, 80, 85],
        "sl_atr_mult":          [1.0, 1.5, 2.0],
        "tp1_rr":               [1.5, 2.0],      # must be ≥ risk_reward_min 1.5 (scalping)
        "rr":                   [2.0, 2.5, 3.0], # tp2 full close
        "max_spread_pips":      [2.0, 3.0, 4.0],
    },
    "macd_ema_trend": {
        "macd_fast":           [9, 12],
        "macd_slow":           [21, 26],
        "macd_signal":         [6, 9],
        "ema_fast":            [15, 20],
        "ema_slow":            [45, 50],
        "atr_period":          [10, 14],
        "sl_atr_mult":         [0.8, 1.0, 1.2, 1.5],
        "max_entry_atr_dist":  [0.5, 1.0, 1.5],   # tighter = fewer but fresher entries
        "vol_confirm_mult":    [0, 1.0, 1.2],      # 0 = disabled (indices/crypto noise)
        "tp1_rr":              [1.5, 1.8, 2.0],   # must be ≥ risk_reward_min 1.5 (day_trading)
        "tp2_rr":              [2.0, 2.5, 3.0],
    },
    "sr_breakout": {
        "lookback_bars":    [30, 50, 70],
        "atr_period":       [10, 14, 20],
        "sl_buffer_atr":    [0.2, 0.3, 0.5],
        "tp1_rr":           [1.5, 1.8, 2.0],   # must be ≥ risk_reward_min 1.5 (day_trading)
        "tp_rr":            [2.0, 2.5, 3.0],   # tp2 runner
        "min_touches":      [2, 3],
        "retest_mode":      [True, False],      # retest entry = tighter SL, better price for forex
        "vol_confirm_mult": [0, 1.0, 1.2],     # 0 = disable MT5 volume filter (noise on H1 forex)
        "rsi_confirm":      [48, 52, 55],      # bull: RSI > this; bear: RSI < (100-this)
        "atr_vol_filter":   [0, 0.6, 0.8],    # 0 = disable ATR floor (helps London open entries)
        "session_open":     [0, 2, 7],         # 0=all-day, 2=Tokyo open, 7=London open
        "session_close":    [0, 21, 22],       # 0=all-day, 21=NY end, 22=forex daily close
    },
    "rsi_divergence": {
        "rsi_period":           [10, 14, 18],
        "ema_bias_period":      [40, 50, 65],
        "divergence_lookback":  [30, 40, 60],   # M30 bars: 30=15h, 40=20h, 60=30h
        "sl_atr_mult":          [1.0, 1.5, 2.0],
        "tp_rr":                [1.5, 1.8, 2.0],   # tp1 — must be ≥ risk_reward_min 1.5
        "tp2_rr":               [2.0, 2.5, 3.0],   # tp2 (full close)
    },
    "ema_trend_rider": {
        "ema_fast":     [13, 20, 25],
        "ema_slow":     [50, 60, 75],
        "sl_atr_mult":  [1.0, 1.5, 2.0],
        "tp1_rr":       [1.5, 2.0, 2.5],      # must be ≥ risk_reward_min 1.5 (swing)
        "tp_rr":        [2.5, 3.0, 3.5],      # tp2 (full close)
    },
    "fibonacci_rsi": {
        "rsi_period":       [10, 14, 18],
        "impulse_lookback": [30, 50, 70],
        "fib_entry_low":    [0.382, 0.5],       # lower bound of golden zone (shallower entry)
        "fib_entry_high":   [0.618, 0.786],     # upper bound of golden zone (deeper entry)
        "sl_atr_mult":      [1.0, 1.5, 2.0],
        "candle_confirm":   [True, False],      # 1-bar H4 confirmation; False suits fast-moving assets
        "tp1_rr":           [1.5, 2.0, 2.5],   # must be ≥ risk_reward_min 1.5 (swing)
        "tp2_rr":           [2.0, 2.5, 3.0],   # fallback R-multiple when structural target is too close
    },
    "weekly_breakout": {
        "atr_break_mult": [0.3, 0.5, 0.7],    # was lookback_bars (dead key — code reads atr_break_mult)
        "adx_threshold":  [18, 20, 25],       # key that code actually reads
        "sl_atr_mult":    [1.0, 1.5, 2.0, 2.5],
        "tp1_rr":         [1.5, 2.0, 2.5],   # must be ≥ risk_reward_min 1.5 (swing)
        "tp_rr":          [2.0, 2.5, 3.0],   # tp2 (full close)
    },
}

# Single-df dispatch for backtesting (secondary TFs not available during replay)
_DISPATCH: dict[str, Callable] = {
    # Each lambda accepts (strategy, primary_df, extra_dfs) where extra_dfs is a
    # dict of secondary timeframe DataFrames keyed by kwarg name.
    # Defaults to {} so old single-TF call sites still work.
    "ema_scalp":          lambda s, df, e={}: s.calculate(df, df_m15=e.get("df_m15")),  # BUG-BT-1: was df_m5
    "bb_squeeze":         lambda s, df, e={}: s.calculate(df),
    "vwap_reversion":     lambda s, df, e={}: s.calculate(df),
    "stoch_rsi_pullback": lambda s, df, e={}: s.calculate(df),  # BUG-BT-2: was missing
    "macd_ema_trend":     lambda s, df, e={}: s.calculate(df, df_h1=e.get("df_h1")),
    "rsi_divergence":     lambda s, df, e={}: s.calculate(df, df_h1=e.get("df_h1")),
    "ema_trend_rider":    lambda s, df, e={}: s.calculate(df, df_h4=e.get("df_h4"), df_d1=e.get("df_d1")),
    "fibonacci_rsi":      lambda s, df, e={}: s.calculate(df),
    "weekly_breakout":    lambda s, df, e={}: s.calculate(df, df_daily=e.get("df_daily")),
    "sr_breakout":        lambda s, df, e={}: s.calculate(df),
}

_STRATEGY_MAP: Optional[dict] = None

# Per-strategy regime-classification TF — must stay in sync with
# engine/strategy_runner._REGIME_TF so ATR% thresholds inside the backtest
# match the bar granularity the strategy actually classifies on at runtime.
# H1 is the default for most strategies; H4 for the two weekly/fib swing
# strategies; M15 for vwap_reversion (mean-reversion needs intraday resolution).
_STRATEGY_REGIME_TF: dict[str, str] = {
    # scalping
    "ema_scalp":           "H1",
    "bb_squeeze":          "H1",
    "vwap_reversion":      "M15",
    "stoch_rsi_pullback":  "H1",
    # day trading
    "macd_ema_trend":      "H1",
    "sr_breakout":         "H1",
    "rsi_divergence":      "H1",
    # swing
    "ema_trend_rider":     "H1",
    "fibonacci_rsi":       "H4",
    "weekly_breakout":     "H4",
}


def _get_strategy_map() -> dict:
    global _STRATEGY_MAP
    if _STRATEGY_MAP is None:
        from engine.strategies.scalping.ema_scalp            import EMAScalp
        from engine.strategies.scalping.bb_squeeze           import BBSqueeze
        from engine.strategies.scalping.vwap_reversion       import VWAPReversion
        from engine.strategies.scalping.stoch_rsi_pullback   import StochRSIPullback  # BUG-BT-2
        from engine.strategies.day_trading.macd_ema_trend    import MACDEMATrend
        from engine.strategies.day_trading.sr_breakout       import SRBreakout
        from engine.strategies.day_trading.rsi_divergence    import RSIDivergence
        from engine.strategies.swing.ema_trend_rider         import EMATrendRider
        from engine.strategies.swing.fibonacci_rsi           import FibonacciRSI
        from engine.strategies.swing.weekly_breakout         import WeeklyBreakout
        _STRATEGY_MAP = {
            "ema_scalp":          EMAScalp,
            "bb_squeeze":         BBSqueeze,
            "vwap_reversion":     VWAPReversion,
            "stoch_rsi_pullback": StochRSIPullback,  # BUG-BT-2
            "macd_ema_trend":     MACDEMATrend,
            "sr_breakout":        SRBreakout,
            "rsi_divergence":     RSIDivergence,
            "ema_trend_rider":    EMATrendRider,
            "fibonacci_rsi":      FibonacciRSI,
            "weekly_breakout":    WeeklyBreakout,
        }
    return _STRATEGY_MAP


# ── Helper functions ─────────────────────────────────────────────────────────

def _grid_combos(strategy_name: str) -> list[dict]:
    """Return parameter combos for a strategy, capped at MAX_GRID_COMBOS.

    For grids that fit within the cap, all valid combos are returned.
    For larger grids, Latin Hypercube Sampling is used: each parameter value
    appears roughly equally often across the sample, providing better coverage
    than pure random selection which can cluster around similar values.
    """
    grid = PARAM_GRIDS.get(strategy_name, {})
    if not grid:
        return [{}]
    keys   = list(grid.keys())
    values = list(grid.values())
    all_combos = [
        dict(zip(keys, combo))
        for combo in itertools.product(*values)
        if _valid_combo(strategy_name, dict(zip(keys, combo)))
    ]
    if len(all_combos) <= MAX_GRID_COMBOS:
        return all_combos

    # Latin Hypercube Sampling: build MAX_GRID_COMBOS slots where every
    # parameter value appears roughly n/k times (n=slots, k=values per param),
    # then shuffle each dimension independently before zipping.
    # This guarantees coverage across all parameter axes instead of random clustering.
    rng = np.random.default_rng(42)
    n = MAX_GRID_COMBOS
    columns: list[list] = []
    for vals in values:
        k = len(vals)
        repeats = (n + k - 1) // k           # ceil(n/k) repetitions
        col = (list(vals) * repeats)[:n]     # trim to exactly n slots
        perm = rng.permutation(n)
        col  = [col[int(i)] for i in perm]   # shuffle this dimension independently
        columns.append(col)

    # Zip dimensions into combos, dedup and filter invalid combinations
    seen: set = set()
    candidates: list[dict] = []
    for row in zip(*columns):
        combo = dict(zip(keys, row))
        key   = tuple(combo[k] for k in keys)
        if key not in seen and _valid_combo(strategy_name, combo):
            seen.add(key)
            candidates.append(combo)

    # Top up with random valid combos if filtering reduced the count below cap
    if len(candidates) < n:
        used   = {tuple(c[k] for k in keys) for c in candidates}
        extras = [c for c in all_combos if tuple(c[k] for k in keys) not in used]
        perm2  = rng.permutation(len(extras))
        extras = [extras[int(i)] for i in perm2]
        candidates += extras[: n - len(candidates)]

    return candidates


def _valid_combo(strategy_name: str, combo: dict) -> bool:
    """Filter logically invalid parameter combinations."""
    # EMA fast must be strictly less than slow
    fast = combo.get("ema_fast", 0)
    slow = combo.get("ema_slow", 0)
    if fast and slow and fast >= slow:
        return False
    # MACD fast must be strictly less than slow
    if strategy_name == "macd_ema_trend":
        if combo.get("macd_fast", 0) >= combo.get("macd_slow", 0):
            return False
        # TP1 (partial close) must be less than TP2 (full close)
        if combo.get("tp1_rr", 0) >= combo.get("tp2_rr", float("inf")):
            return False
    # Strategies with tp1_rr / tp_rr naming: tp1 must be less than tp2
    if strategy_name in ("sr_breakout", "ema_trend_rider", "weekly_breakout"):
        if combo.get("tp1_rr", 0) >= combo.get("tp_rr", float("inf")):
            return False
    # rsi_divergence: tp_rr is tp1 (partial close), tp2_rr is tp2 (full close)
    if strategy_name == "rsi_divergence":
        if combo.get("tp_rr", 0) >= combo.get("tp2_rr", float("inf")):
            return False
    # fibonacci_rsi: tp1 must be less than tp2; golden zone low% must be less than high%
    if strategy_name == "fibonacci_rsi":
        if combo.get("tp1_rr", 0) >= combo.get("tp2_rr", float("inf")):
            return False
        if combo.get("fib_entry_low", 0) >= combo.get("fib_entry_high", float("inf")):
            return False
    # bb_squeeze: tp1_atr_mult (partial close) must be less than tp_atr_mult (full close)
    if strategy_name == "bb_squeeze":
        if combo.get("tp1_atr_mult", 0) >= combo.get("tp_atr_mult", float("inf")):
            return False
    # vwap_reversion: stop-loss band must be wider than entry band
    if strategy_name == "vwap_reversion":
        if combo.get("sigma_entry", 0) >= combo.get("sigma_sl", float("inf")):
            return False
    # stoch_rsi_pullback: tp1 (partial close) must be less than tp2 (full close)
    if strategy_name == "stoch_rsi_pullback":
        if combo.get("tp1_rr", 0) >= combo.get("rr", float("inf")):
            return False
    return True


def _simulate_trade(
    df: pd.DataFrame, idx: int,
    direction: str, entry: float, sl: float, tp: float,
    max_hold: int,
) -> tuple[bool, float]:
    """Walk forward from bar idx+1 to determine if SL or TP hits first.
    Returns (won, rr_achieved)."""
    if tp == 0 or sl == 0 or entry == 0:
        return False, 0.0
    risk   = abs(entry - sl)
    reward = abs(tp   - entry)
    if risk == 0:
        return False, 0.0
    target_rr = reward / risk

    end = min(idx + 1 + max_hold, len(df))
    for j in range(idx + 1, end):
        high = float(df.iloc[j]["high"])
        low  = float(df.iloc[j]["low"])
        if direction == "BUY":
            if low  <= sl: return False, -1.0
            if high >= tp: return True,  target_rr
        else:
            if high >= sl: return False, -1.0
            if low  <= tp: return True,  target_rr

    # Timeout: close at last available bar
    close_px  = float(df.iloc[end - 1]["close"])
    actual    = (close_px - entry) if direction == "BUY" else (entry - close_px)
    rr_actual = actual / risk
    return actual > 0, rr_actual


def _classify_bar_regime(df_slice: pd.DataFrame, symbol: str, timeframe: str = "h1") -> str:
    """Classify regime at a backtest bar. Lightweight — no hysteresis in backtest."""
    try:
        from engine.regime_classifier import _classify_raw
        return _classify_raw(df_slice, symbol, timeframe)
    except Exception:
        return "unknown"


def _backtest_combo(
    strategy_cls,
    dispatch_fn,
    df: pd.DataFrame,
    params: dict,
    step: int,
    max_hold: int,
    warmup: int,
    spread_r: float = 0.0,
    symbol: str = "__bt__",
    extra_dfs: "dict | None" = None,
    bt_window: int = 500,
    trading_type: str = "scalping",
    regime_tf: str = "h1",
) -> tuple[float, float, int, dict[str, dict]]:
    """Walk-forward backtest one param combo.
    Returns (win_rate, avg_rr, n_trades, regime_stats).
    regime_stats: {regime_label: {wins, total, rr_sum}}
    spread_r deducted from each trade result to simulate round-trip spread+slippage.
    extra_dfs: optional secondary timeframe DataFrames keyed by kwarg name (e.g. df_h1).
               Each is time-sliced at bar i to avoid lookahead bias.
    bt_window: fixed history length passed to the strategy at each step. Replaces the
               old df.iloc[:i+1] growing slice with df.iloc[i+1-bt_window:i+1], turning
               O(n²) into O(n). Safety: slice still ends at bar i — no future data.
               Only old history is trimmed from the front, which never introduces lookahead."""
    wins: list[float] = []
    rrs:  list[float] = []
    regime_stats: dict[str, dict] = {}   # label → {wins, total, rr_sum}
    _has_time = "time" in df.columns

    # vwap_reversion (and any future session-aware strategy) requires a proper
    # datetime "time" column to compute daily VWAP. Without it the strategy will
    # raise on every bar, causing an infinite silent-exception loop. Bail early
    # with an empty result so the optimizer marks this combo as 0-signal instead
    # of spinning for the full dataset duration.
    _strat_name = strategy_cls.__name__ if hasattr(strategy_cls, "__name__") else ""
    _needs_time = getattr(strategy_cls, "_requires_time_column", False) or (
        _strat_name in ("VWAPReversion",)
    )
    if _needs_time and not _has_time:
        logger.warning(
            f"_backtest_combo: {_strat_name} requires 'time' column but df lacks it — "
            "skipping combo (pass OHLCV from MT5Client which always includes 'time')"
        )
        return 0.0, 0.0, 0, {}

    # Pre-compute VWAP columns on the FULL df ONCE before the bar loop.
    # df.iloc[a:b] slices are views — they see columns added to the parent df,
    # so _ensure_vwap's cache check ("vwap" in df.columns) will be True on
    # every slice without any recomputation. This is the real O(1) fix.
    # Safety: VWAP uses only past bars (cumsum per day group) — no lookahead.
    if _strat_name == "VWAPReversion" and _has_time:
        try:
            from engine.strategies.scalping.vwap_reversion import _ensure_vwap
            df = df.copy()
            # Ensure typical column exists before VWAP pre-compute
            if "typical" not in df.columns:
                df["typical"] = (df["high"] + df["low"] + df["close"]) / 3.0
            vwap_window = params.get("vwap_std_window", 20)
            _ensure_vwap(df, vwap_window)
        except Exception as _ve:
            logger.debug(f"VWAP pre-compute failed: {_ve}")

    i = warmup

    while i < len(df) - 1:
        try:
            strat  = strategy_cls(symbol="__bt__", params=params)
            # Fixed-width window: cut old history from the front only.
            # Slice still ends at bar i → strategy cannot see bar i+1 or beyond.
            _win_start = max(0, i + 1 - bt_window)
            # Build time-sliced extra dfs at this bar to prevent lookahead bias
            _e: dict = {}
            if extra_dfs:
                if _has_time:
                    _bar_time = df.iloc[i]["time"]
                    for _k, _v in extra_dfs.items():
                        if "time" in _v.columns:
                            # Step 1: exclude all future bars (same guarantee as before)
                            _tmp = _v[_v["time"] <= _bar_time]
                            # Step 2: trim old history from front for O(n) performance
                            _e[_k] = _tmp.iloc[max(0, len(_tmp) - bt_window):]
                        else:
                            _e[_k] = _v.iloc[max(0, i + 1 - bt_window):i + 1]
                else:
                    for _k, _v in extra_dfs.items():
                        _e[_k] = _v.iloc[max(0, i + 1 - bt_window):i + 1]
            result = dispatch_fn(strat, df.iloc[_win_start:i + 1], _e)
            sig    = result.signal
            if sig and sig.direction in ("BUY", "SELL") and sig.tp_price:
                won, rr = _simulate_trade(
                    df, i, sig.direction,
                    sig.entry_price, sig.sl_price, sig.tp_price or 0.0,
                    max_hold,
                )
                # Apply spread cost: reduces R:R of wins and deepens losses
                rr_adj = rr - spread_r
                wins.append(float(rr_adj > 0))
                rrs.append(rr_adj)

                # Record per-regime outcome
                regime = _classify_bar_regime(df.iloc[_win_start:i + 1], symbol, regime_tf)
                rs = regime_stats.setdefault(regime, {"wins": 0, "total": 0, "rr_sum": 0.0})
                rs["total"] += 1
                if rr_adj > 0:
                    rs["wins"] += 1
                rs["rr_sum"] += rr_adj

                i += max(max_hold // 4, step)
                continue
        except Exception as _exc:
            logger.debug(f"Backtest signal skipped at bar {i} ({strategy_cls.__name__}/{symbol}): {_exc}")
        i += step

    n = len(wins)
    min_signals = MIN_BACKTEST_SIGNALS.get(trading_type, 10)
    if n < min_signals:
        return 0.0, 0.0, n, {}
    win_rate = float(np.mean(wins))
    avg_rr   = float(np.mean(rrs))
    return win_rate, avg_rr, n, regime_stats


# ── Optimizer class ──────────────────────────────────────────────────────────

class ParamOptimizer:
    """
    Singleton that manages strategy parameter optimization.
    Thread-safe — all public methods can be called from background threads.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._running: set[str] = set()  # keys currently being optimized
        self._queue: collections.deque = collections.deque()  # (strategy_name, symbol, df, trading_type) tuples pending
        self._status: dict[str, dict] = {}  # key → status dict
        self._load_status()

    # ── Public API ────────────────────────────────────────────────────────────
    @staticmethod
    def _key(strategy_name: str, symbol: str) -> str:
        return f"{strategy_name}__{symbol.rstrip('#+*!')}"
    
    def optimize_async(
        self,
        strategy_name: str,
        symbol: str,
        df: pd.DataFrame,
        trading_type: str,
    ) -> bool:
        """Start background optimization or queue if at limit. Returns False if already running/queued (duplicate)."""
        key = self._key(strategy_name, symbol)
        with self._lock:
            # Prevent duplicates: already running or already in queue
            if key in self._running:
                logger.info(f"Optimizer already running for {key}")
                return False
            if any(item[1] == symbol and item[0] == strategy_name for item in self._queue):
                logger.info(f"Optimizer already queued for {key}")
                return False
            
            # If we have capacity, start immediately
            if len(self._running) < MAX_CONCURRENT_OPT:
                self._running.add(key)
                t = threading.Thread(
                    target=self._optimize,
                    args=(strategy_name, symbol, df, trading_type, key),
                    daemon=True,
                )
                t.start()
                logger.info(f"Param optimizer started: {strategy_name}/{symbol} ({len(df)} bars)")
                return True
            else:
                # Queue it for later
                self._queue.append((strategy_name, symbol, df, trading_type))
                logger.info(f"Optimizer queued (position {len(self._queue)}): {key}")
                return True

    def get_params(self, strategy_name: str, symbol: str = "", regime: str | None = None) -> dict:
        """Return best known params for a strategy+symbol.

        Resolution order:
          1. Per-regime params: data[strategy][symbol]["by_regime"][regime]  (if regime given)
          2. Per-symbol global best: data[strategy][symbol]
          3. Global best: data[strategy]["__global__"]
          4. Empty dict (use strategy defaults)
        """
        with self._lock:
            data = self._load_opt()
        strat = data.get(strategy_name, {})
        clean = symbol.rstrip("#+*!")
        sym_entry = strat.get(clean) or strat.get("__global__") or {}

        if regime and isinstance(sym_entry, dict):
            by_regime = sym_entry.get("by_regime", {})
            regime_params = by_regime.get(regime, {})
            if regime_params:
                return regime_params

        # Strip the by_regime sub-dict before returning, expose flat params
        flat = {k: v for k, v in sym_entry.items() if k != "by_regime"}
        return flat or {}

    def should_reoptimize(self, strategy_name: str, symbol: str) -> bool:
        """
        True if:
        - Never been optimized for this pair, OR
        - Last optimized > BACKTEST_COOLDOWN_HOURS ago AND recent win_rate low
        """
        key = self._key(strategy_name, symbol)
        with self._lock:
            info = self._status.get(key, {})

        last = info.get("last_optimized_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            # Ensure timezone-aware so subtraction with utcnow never raises TypeError
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.warning(f"Optimizer: invalid timestamp for {strategy_name}/{symbol}: {last!r}")
            return True
        hours_ago = (
            datetime.now(tz=timezone.utc) -
            last_dt
        ).total_seconds() / 3600
        if hours_ago < BACKTEST_COOLDOWN_HOURS:
            return False

        # Check if live win_rate has fallen — if so, re-optimize despite cooldown
        from ai.trade_memory import memory
        from engine.account_store import current_mode as _cur_mode_opt
        outcomes = [
            o for o in memory.recent(n=50, live_only=True, mode=_cur_mode_opt())
            if o.get("strategy") == strategy_name and o.get("symbol") == symbol
        ]
        # Count trades that closed AFTER the last optimization
        _new_trades = outcomes
        if last:
            try:
                _last_dt_filter = datetime.fromisoformat(last)
                if _last_dt_filter.tzinfo is None:
                    _last_dt_filter = _last_dt_filter.replace(tzinfo=timezone.utc)
                _new_trades = [
                    o for o in outcomes
                    if o.get("close_time") and
                    datetime.fromisoformat(
                        o["close_time"].replace("Z", "+00:00")
                    ) > _last_dt_filter
                ]
            except Exception:
                pass

        # Require at least 30 new trades AND win rate below threshold.
        # 30 trades = ~2 weeks of day trading = enough to distinguish
        # regime shift from normal variance.
        _MIN_NEW_TRADES_FOR_REOPT = 30
        if len(_new_trades) >= _MIN_NEW_TRADES_FOR_REOPT:
            wins = sum(1 for o in _new_trades if o["profit"] > 0)
            if wins / len(_new_trades) < LIVE_REFINE_WIN_THRESH:
                return True
        return False

    def status(self) -> dict:
        with self._lock:
            completed = dict(self._status)
            running   = set(self._running)
            queued    = [(s, sym) for s, sym, _, _ in self._queue]
        result = {k: {**v, "running": False, "queued": False} for k, v in completed.items()}
        for key in running:
            if key in result:
                result[key]["running"] = True
            else:
                parts = key.split("__", 1)
                result[key] = {
                    "strategy": parts[0] if parts else key,
                    "symbol":   parts[1] if len(parts) > 1 else "",
                    "running":  True,
                    "queued":   False,
                }
        for strat, sym in queued:
            key = f"{strat}__{sym}"
            if key in result:
                result[key]["queued"] = True
            else:
                result[key] = {
                    "strategy": strat,
                    "symbol":   sym,
                    "running":  False,
                    "queued":   True,
                }
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _process_queue(self) -> None:
        """Pop next job from queue and start it if there's capacity."""
        with self._lock:
            if not self._queue or len(self._running) >= MAX_CONCURRENT_OPT:
                return
            strategy_name, symbol, df, trading_type = self._queue.popleft()
            key = self._key(strategy_name, symbol)
            self._running.add(key)
        t = threading.Thread(
            target=self._optimize,
            args=(strategy_name, symbol, df, trading_type, key),
            daemon=True,
        )
        t.start()
        logger.info(f"Param optimizer started from queue: {strategy_name}/{symbol} ({len(df)} bars)")

    def _optimize(
        self,
        strategy_name: str,
        symbol: str,
        df: pd.DataFrame,
        trading_type: str,
        key: str,
    ) -> None:
        try:
            best_params, score, n, regime_best_params = self._run_backtest(
                strategy_name, symbol, df, trading_type
            )
            if best_params is not None and score > 0.0:
                self._save_params(strategy_name, symbol, best_params, regime_best_params)
                with self._lock:
                    self._status[key] = {
                        "strategy":          strategy_name,
                        "symbol":            symbol,
                        "trading_type":      trading_type,
                        "best_score":        round(score, 4),
                        "n_signals":         n,
                        "best_params":       best_params,
                        "regime_params":     regime_best_params,
                        "last_optimized_at": datetime.now(tz=timezone.utc).isoformat(),
                        "bars_used":         len(df),
                    }
                self._save_status()
                logger.info(
                    f"Optimizer: {strategy_name}/{symbol} best_score={score:.3f} "
                    f"params={best_params} regimes={list(regime_best_params.keys())}"
                )
                # Notify user of optimizer completion
                notification_manager.add(
                    type="optimizer_complete",
                    title="Optimization Complete",
                    message=f"{self._key(strategy_name, symbol)} optimized for {symbol} (score: {score:.3f})",
                    severity="success",
                    metadata={
                        "strategy": strategy_name,
                        "symbol": symbol,
                        "trading_type": trading_type,
                        "best_score": round(score, 3),
                        "best_params": best_params
                    }
                )
            else:
                logger.warning(f"Optimizer: no valid combos found for {strategy_name}/{symbol}")
                with self._lock:
                    self._status[key] = {
                        "strategy":           strategy_name,
                        "symbol":             symbol,
                        "trading_type":       trading_type,
                        "last_attempted_at":  datetime.now(tz=timezone.utc).isoformat(),
                        "best_score":         0.0,
                        "bars_used":          len(df),
                    }
                self._save_status()
        except Exception as exc:
            logger.exception(f"Optimizer error {strategy_name}/{symbol}: {exc}")
        finally:
            with self._lock:
                self._running.discard(key)
            # Try to process queued jobs now that a slot is free
            self._process_queue()

    def _run_backtest(
        self,
        strategy_name: str,
        symbol: str,
        df: pd.DataFrame,
        trading_type: str,
    ) -> tuple[Optional[dict], float, int, dict[str, dict]]:
        """Run grid-search backtest. Returns (best_params, best_score, n_trades, regime_best_params).
        regime_best_params: {regime_label: best_params_for_that_regime}"""
        strat_map    = _get_strategy_map()
        strategy_cls = strat_map.get(strategy_name)
        if strategy_cls is None:
            logger.warning(f"Optimizer: unknown strategy {strategy_name}")
            return None, 0.0, 0, {}

        dispatch  = _DISPATCH.get(strategy_name, lambda s, d, e={}: s.calculate(d))
        combos    = _grid_combos(strategy_name)
        cfg       = _BACKTEST_CONFIG.get(trading_type, _BACKTEST_CONFIG["day_trading"])
        regime_tf = _STRATEGY_REGIME_TF.get(strategy_name, "H1")

        cfg = dict(cfg)   # copy so we don't mutate the module-level constant
        if _BT_STEP_STRATEGY and strategy_name in _BT_STEP_STRATEGY:
            cfg["step"] = _BT_STEP_STRATEGY[strategy_name]
            
        # Walk-forward validation: split df into train (first 75%) and
        # validation (last 25%) periods. Optimize params on train only,
        # then score each candidate on validation to measure generalization.
        # This prevents selecting params that overfit to the full history.
        _val_split = int(len(df) * 0.75)
        _df_train = df.iloc[:_val_split].reset_index(drop=True)
        _df_val   = df.iloc[_val_split:].reset_index(drop=True)
        # Require minimum bars in each split
        _min_bars = max(cfg["warmup"] * 2, 100)
        _use_validation = len(_df_train) >= _min_bars and len(_df_val) >= _min_bars
        if not _use_validation:
            _df_train = df   # fallback: dataset too short for split
            _df_val   = df
            logger.debug(f"Optimizer: dataset too short for walk-forward split ({len(df)} bars) — using full history")
        
        best_params: Optional[dict] = None
        best_score  = -1.0
        best_n      = 0

        # Per-regime tracking: {regime: (best_score, best_params)}
        regime_best: dict[str, tuple[float, dict]] = {}
        clean = symbol.rstrip("#+*!")
        spread_r  = SPREAD_COST_R_SYMBOL.get(clean, SPREAD_COST_R.get(trading_type, 0.05))
        bt_window = _BT_WINDOW_STRATEGY.get(strategy_name, _BT_WINDOW.get(trading_type, 500))

        # Build secondary-timeframe extras for strategies that need them.
        _extra_dfs: dict = {}
        if strategy_name in ("macd_ema_trend", "rsi_divergence"):
            _extra_dfs = {"df_h1": df}
        elif strategy_name == "ema_scalp":
            _extra_dfs = {"df_m15": df}
        elif strategy_name == "ema_trend_rider":
            _extra_dfs = {"df_h4": df, "df_d1": df}
        elif strategy_name == "weekly_breakout":
            _extra_dfs = {"df_daily": df}

        # Slice extra_dfs to match the train/val boundary so the secondary-TF
        # dataframe cannot leak future bars into the training backtest.
        if _use_validation and _extra_dfs:
            _extra_dfs_train = {
                k: v.iloc[:_val_split].reset_index(drop=True)
                for k, v in _extra_dfs.items()
            }
            _extra_dfs_val = {
                k: v.iloc[_val_split:].reset_index(drop=True)
                for k, v in _extra_dfs.items()
            }
        else:
            _extra_dfs_train = _extra_dfs
            _extra_dfs_val   = _extra_dfs

        for combo in combos:
            time.sleep(0)  # yield CPU between combos to prevent event-loop starvation
            # Phase 1: optimize on training set (train-split extra_dfs only)
            wr_train, avg_rr_train, n_train, regime_stats = _backtest_combo(
                strategy_cls, dispatch, _df_train, combo,
                cfg["step"], cfg["max_hold"], cfg["warmup"], spread_r, symbol,
                _extra_dfs_train, bt_window, trading_type, regime_tf,
            )
            min_signals = MIN_BACKTEST_SIGNALS.get(trading_type, 10)
            train_score = wr_train * max(avg_rr_train, 0.0) if n_train >= min_signals else 0.0
            if train_score <= 0.0:
                continue  # skip combos that don't work on training data

            # Phase 2: validate on held-out period
            if _use_validation:
                wr_val, avg_rr_val, n_val, _ = _backtest_combo(
                    strategy_cls, dispatch, _df_val, combo,
                    cfg["step"], cfg["max_hold"], cfg["warmup"], spread_r, symbol,
                    _extra_dfs_val, bt_window, trading_type, regime_tf,
                )
                val_score = wr_val * max(avg_rr_val, 0.0) if n_val >= max(min_signals // 3, 3) else 0.0
                score = 0.4 * train_score + 0.6 * val_score
            else:
                score = train_score
                n_val = n_train

            if score > best_score:
                best_score  = score
                best_params = combo
                best_n      = n_train + n_val

            # Track per-regime best combo
            for regime_label, rs in regime_stats.items():
                if rs["total"] >= max(min_signals // 3, 5):
                    rwr    = rs["wins"] / rs["total"]
                    ravg   = rs["rr_sum"] / rs["total"]
                    rscore = rwr * max(ravg, 0.0)
                    prev_score = regime_best.get(regime_label, (-1.0, {}))[0]
                    if rscore > prev_score:
                        regime_best[regime_label] = (rscore, combo)

        regime_best_params = {lbl: params for lbl, (_, params) in regime_best.items()}
        return best_params, best_score, best_n, regime_best_params

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_opt(self) -> dict:
        """Load optimized params from disk with archive recovery on corruption."""
        if not OPT_FILE.exists():
            return {}
        try:
            _text = OPT_FILE.read_text(encoding="utf-8").strip()
            if not _text:
                raise ValueError("file is empty")
            return json.loads(_text)
        except Exception as exc:
            logger.warning(
                f"Optimizer: could not load params file ({exc}) — "
                "attempting archive recovery"
            )
            _archive_dir = OPT_FILE.parent / "params_archive"
            _archives = sorted(_archive_dir.glob("optimized_params_*.json")) \
                if _archive_dir.exists() else []
            for _arc in reversed(_archives):
                try:
                    _arc_text = _arc.read_text(encoding="utf-8").strip()
                    if not _arc_text:
                        continue
                    _data = json.loads(_arc_text)
                    import shutil as _sh
                    _sh.copy2(_arc, OPT_FILE)
                    logger.info(
                        f"Optimizer: restored params from archive {_arc.name} "
                        f"({len(_data)} strategies recovered)"
                    )
                    return _data
                except Exception:
                    continue
            try:
                OPT_FILE.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning(
                "Optimizer: no valid archive found — corrupt params file removed. "
                "Re-run run_optimizer.py to rebuild."
            )
            return {}

    def _save_params(
        self,
        strategy_name: str,
        symbol: str,
        params: dict,
        regime_params: dict[str, dict] | None = None,
    ) -> None:
        with self._lock:
            try:
                import shutil as _shutil
                _archive_dir = OPT_FILE.parent / "params_archive"
                _archive_dir.mkdir(exist_ok=True)
                if OPT_FILE.exists():
                    _ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
                    _archive_path = _archive_dir / f"optimized_params_{_ts}.json"
                    _shutil.copy2(OPT_FILE, _archive_path)
                    _archives = sorted(_archive_dir.glob("optimized_params_*.json"))
                    for _old in _archives[:-5]:
                        _old.unlink(missing_ok=True)
            except Exception as _arc_exc:
                logger.debug(f"Optimizer: archive failed (non-critical): {_arc_exc}")

            data = self._load_opt()
            data.setdefault(strategy_name, {})
            existing = data[strategy_name].get(symbol, {})
            existing_by_regime = existing.get("by_regime", {}) if isinstance(existing, dict) else {}
            if regime_params:
                existing_by_regime.update(regime_params)
            entry = dict(params)
            # Safety: clamp any R:R parameters to a minimum of 1.0
            _RR_KEYS = {"rr", "tp_rr", "tp1_rr", "tp2_rr", "tp1_atr_mult", "tp_atr_mult"}
            _MIN_RR  = 1.0
            for _k in _RR_KEYS:
                if _k in entry and isinstance(entry[_k], (int, float)) and entry[_k] < _MIN_RR:
                    logger.warning(
                        f"Optimizer: clamping {strategy_name}/{symbol} {_k}={entry[_k]:.3f} → {_MIN_RR} "
                        f"(below minimum R:R)"
                    )
                    entry[_k] = _MIN_RR
            if existing_by_regime:
                entry["by_regime"] = existing_by_regime
            clean = symbol.rstrip("#+*!")
            data[strategy_name][clean] = entry
            if "__global__" not in data[strategy_name]:
                data[strategy_name]["__global__"] = entry
            # Atomic write
            import os as _os, tempfile as _tempfile
            _serialised = json.dumps(data, indent=2)
            _fd, _tmp_path = _tempfile.mkstemp(dir=OPT_FILE.parent, suffix=".tmp")
            try:
                with _os.fdopen(_fd, "w", encoding="utf-8") as _tf:
                    _tf.write(_serialised)
                _os.replace(_tmp_path, OPT_FILE)
            except Exception:
                try:
                    _os.unlink(_tmp_path)
                except OSError:
                    pass
                raise

    _STATUS_FILE = Path(__file__).parent / "data" / "optimizer_status.json"

    def _load_status(self) -> None:
        try:
            if self._STATUS_FILE.exists():
                self._status = json.loads(self._STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._status = {}

    def _save_status(self) -> None:
        try:
            import os as _os, tempfile as _tempfile
            self._STATUS_FILE.parent.mkdir(exist_ok=True)
            _serialised = json.dumps(self._status, indent=2)
            _fd, _tmp = _tempfile.mkstemp(dir=self._STATUS_FILE.parent, suffix=".tmp")
            try:
                with _os.fdopen(_fd, "w", encoding="utf-8") as _tf:
                    _tf.write(_serialised)
                _os.replace(_tmp, self._STATUS_FILE)
            except Exception:
                try:
                    _os.unlink(_tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning(f"Optimizer: could not save status: {exc}")


# Application singleton
optimizer = ParamOptimizer()
