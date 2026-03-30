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

import itertools
import json
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
MIN_BACKTEST_SIGNALS      = 10   # discard combos that fired fewer signals (≥10 allows low-frequency strategies like sr_breakout on commodities)
MAX_GRID_COMBOS           = 64   # cap to keep backtest fast
BACKTEST_COOLDOWN_HOURS   = 24   # min hours between automatic re-backtests
LIVE_REFINE_WIN_THRESH    = 0.45 # re-optimize when win_rate drops below this
MAX_CONCURRENT_OPT        = 2    # max simultaneous optimizer jobs (prevents CPU starvation / MT5 disconnect)

# Spread cost in R-units (round-trip bid/ask spread + typical slippage per mode).
# This is subtracted from each simulated trade so unrealistic tight-spread combos
# are penalised during grid search.
SPREAD_COST_R: dict[str, float] = {
    "scalping":    0.15,   # tight stops → spread ~15% of 1R
    "day_trading": 0.05,   # spread ~5% of 1R
    "swing":       0.02,   # spread ~2% of 1R for wide swing targets
}

# Per-symbol overrides: these assets have much wider spreads than typical forex.
# Checked first in _run_backtest(); falls back to SPREAD_COST_R[trading_type] if symbol absent.
SPREAD_COST_R_SYMBOL: dict[str, float] = {
    "BTCUSD":    0.30,  "ETHUSD":    0.30,   # crypto: spread can be 0.3R easily
    "XRPUSD":    0.25,  "SOLUSD":    0.25,
    "XAUUSD":    0.10,  "GOLD":      0.10,   # gold: tighter than crypto, wider than forex
    "SILVER":    0.12,  "USOIL":     0.12,   "UKOIL":     0.12,
    "US30Cash":  0.08,  "US100Cash": 0.08,   "US500Cash": 0.08,   # US indices
    "GER40Cash": 0.10,  "UK100Cash": 0.10,   "FRA40Cash": 0.10,   # EU indices
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

# ── Parameter grids ──────────────────────────────────────────────────────────
# Only the most impactful parameters per strategy (keep total combos ≤ MAX_GRID_COMBOS)
PARAM_GRIDS: dict[str, dict[str, list]] = {
    "ema_scalp": {
        "ema_fast": [5, 8, 10, 13],
        "ema_slow": [18, 21, 26, 34],
        "rsi_min":  [40, 45, 50, 52],
        "rsi_max":  [60, 65, 70],
        "sl_pips":  [4, 6, 8, 10, 12],
        "rr":       [1.5, 2.0, 2.5, 3.0],
    },
    "bb_squeeze": {
        "bb_period":       [15, 20, 25],
        "bb_std":          [1.5, 2.0, 2.5],
        "min_squeeze_bars": [3, 5, 7],
        "sl_atr_mult":     [1.0, 1.5, 2.0],
        "tp_atr_mult":     [2.0, 2.5, 3.0],
    },
    "vwap_reversion": {
        "sigma_entry": [1.0, 1.5, 2.0],
        "sigma_sl":    [2.0, 2.5, 3.0],
        "rsi_period":  [7, 9, 14],
    },
    "macd_ema_trend": {
        "macd_fast":   [9, 12],
        "macd_slow":   [21, 26],
        "macd_signal": [6, 9],
        "ema_fast":    [15, 20],
        "ema_slow":    [45, 50],
        "tp1_rr":      [0.8, 1.0, 1.2],
        "tp2_rr":      [1.5, 2.0, 2.5],
    },
    "sr_breakout": {
        "lookback_bars":  [30, 50, 70],
        "atr_period":     [10, 14, 20],
        "sl_buffer_atr":  [0.2, 0.3, 0.5],
        "tp_rr":          [1.5, 1.8, 2.2],
    },
    "rsi_divergence": {
        "rsi_period":      [10, 14, 18],
        "ema_bias_period": [40, 50, 65],
        "tp_rr":           [1.2, 1.5, 2.0],
    },
    "ema_trend_rider": {
        "ema_fast":     [13, 20, 25],
        "ema_slow":     [50, 60, 75],
        "sl_atr_mult":  [1.0, 1.5, 2.0],
        "tp_rr":        [2.0, 2.5, 3.0],
    },
    "fibonacci_rsi": {
        "rsi_period":   [10, 14, 18],
        "fib_lookback": [30, 50, 70],
        "sl_atr_mult":  [1.0, 1.5, 2.0],
    },
    "weekly_breakout": {
        "lookback_bars": [50, 80, 100],
        "sl_atr_mult":   [1.0, 1.5, 2.0, 2.5],
        "tp_rr":         [1.5, 2.0, 2.5],
    },
}

# Single-df dispatch for backtesting (secondary TFs not available during replay)
_DISPATCH: dict[str, Callable] = {
    # Each lambda accepts (strategy, primary_df, extra_dfs) where extra_dfs is a
    # dict of secondary timeframe DataFrames keyed by kwarg name.
    # Defaults to {} so old single-TF call sites still work.
    "ema_scalp":       lambda s, df, e={}: s.calculate(df, df_m5=e.get("df_m5")),
    "bb_squeeze":      lambda s, df, e={}: s.calculate(df),
    "vwap_reversion":  lambda s, df, e={}: s.calculate(df),
    "macd_ema_trend":  lambda s, df, e={}: s.calculate(df, df_h1=e.get("df_h1")),
    "rsi_divergence":  lambda s, df, e={}: s.calculate(df, df_h1=e.get("df_h1")),
    "ema_trend_rider": lambda s, df, e={}: s.calculate(df, df_h4=e.get("df_h4"), df_d1=e.get("df_d1")),
    "fibonacci_rsi":   lambda s, df, e={}: s.calculate(df),
    "weekly_breakout": lambda s, df, e={}: s.calculate(df, df_daily=e.get("df_daily")),
    "sr_breakout":     lambda s, df, e={}: s.calculate(df),
}

_STRATEGY_MAP: Optional[dict] = None


def _get_strategy_map() -> dict:
    global _STRATEGY_MAP
    if _STRATEGY_MAP is None:
        from engine.strategies.scalping.ema_scalp       import EMAScalp
        from engine.strategies.scalping.bb_squeeze      import BBSqueeze
        from engine.strategies.scalping.vwap_reversion  import VWAPReversion
        from engine.strategies.day_trading.macd_ema_trend import MACDEMATrend
        from engine.strategies.day_trading.sr_breakout  import SRBreakout
        from engine.strategies.day_trading.rsi_divergence import RSIDivergence
        from engine.strategies.swing.ema_trend_rider    import EMATrendRider
        from engine.strategies.swing.fibonacci_rsi      import FibonacciRSI
        from engine.strategies.swing.weekly_breakout    import WeeklyBreakout
        _STRATEGY_MAP = {
            "ema_scalp":       EMAScalp,
            "bb_squeeze":      BBSqueeze,
            "vwap_reversion":  VWAPReversion,
            "macd_ema_trend":  MACDEMATrend,
            "sr_breakout":     SRBreakout,
            "rsi_divergence":  RSIDivergence,
            "ema_trend_rider": EMATrendRider,
            "fibonacci_rsi":   FibonacciRSI,
            "weekly_breakout": WeeklyBreakout,
        }
    return _STRATEGY_MAP


# ── Helper functions ─────────────────────────────────────────────────────────

def _grid_combos(strategy_name: str) -> list[dict]:
    """Return all valid param combos for a strategy, capped at MAX_GRID_COMBOS."""
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
    if len(all_combos) > MAX_GRID_COMBOS:
        rng = np.random.default_rng(42)
        idx = rng.choice(len(all_combos), MAX_GRID_COMBOS, replace=False)
        all_combos = [all_combos[int(i)] for i in sorted(idx)]
    return all_combos


def _valid_combo(strategy_name: str, combo: dict) -> bool:
    """Filter logically invalid combinations."""
    fast = combo.get("ema_fast", 0)
    slow = combo.get("ema_slow", 0)
    if fast and slow and fast >= slow:
        return False
    if strategy_name == "macd_ema_trend":
        if combo.get("macd_fast", 0) >= combo.get("macd_slow", 0):
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


def _classify_bar_regime(df_slice: pd.DataFrame, symbol: str) -> str:
    """Classify regime at a backtest bar. Lightweight — no hysteresis in backtest."""
    try:
        from engine.regime_classifier import _classify_raw
        return _classify_raw(df_slice, symbol)
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
                regime = _classify_bar_regime(df.iloc[_win_start:i + 1], symbol)
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
    if n < MIN_BACKTEST_SIGNALS:
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
        self._status: dict[str, dict] = {}  # key → status dict
        self._load_status()

    # ── Public API ────────────────────────────────────────────────────────────

    def optimize_async(
        self,
        strategy_name: str,
        symbol: str,
        df: pd.DataFrame,
        trading_type: str,
    ) -> bool:
        """Start background optimization. Returns False if already running or at concurrency limit."""
        key = f"{strategy_name}__{symbol}"
        with self._lock:
            if key in self._running:
                return False
            if len(self._running) >= MAX_CONCURRENT_OPT:
                logger.info(
                    f"Optimizer at max concurrency ({MAX_CONCURRENT_OPT}), skipping {key}"
                )
                return False
            self._running.add(key)
        t = threading.Thread(
            target=self._optimize,
            args=(strategy_name, symbol, df, trading_type, key),
            daemon=True,
        )
        t.start()
        logger.info(f"Param optimizer started: {strategy_name}/{symbol} ({len(df)} bars)")
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
        sym_entry = strat.get(symbol) or strat.get("__global__") or {}

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
        key = f"{strategy_name}__{symbol}"
        with self._lock:
            info = self._status.get(key, {})

        last = info.get("last_optimized_at")
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
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
        outcomes = [
            o for o in memory.recent(n=50, live_only=True)
            if o.get("strategy") == strategy_name and o.get("symbol") == symbol
        ]
        if len(outcomes) >= MIN_TRADES_FOR_REFINEMENT:
            wins = sum(1 for o in outcomes if o["profit"] > 0)
            if wins / len(outcomes) < LIVE_REFINE_WIN_THRESH:
                return True
        return False

    def status(self) -> dict:
        with self._lock:
            completed = dict(self._status)
            running   = set(self._running)
        # Merge: add a `running` flag to in-progress jobs
        result = {k: {**v, "running": False} for k, v in completed.items()}
        for key in running:
            if key in result:
                result[key]["running"] = True
            else:
                # Job started but no prior record — create a placeholder
                parts = key.split("__", 1)
                result[key] = {
                    "strategy": parts[0] if parts else key,
                    "symbol":   parts[1] if len(parts) > 1 else "",
                    "running":  True,
                }
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

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

        dispatch = _DISPATCH.get(strategy_name, lambda s, d, e={}: s.calculate(d))
        combos   = _grid_combos(strategy_name)
        cfg      = _BACKTEST_CONFIG.get(trading_type, _BACKTEST_CONFIG["day_trading"])

        best_params: Optional[dict] = None
        best_score  = -1.0
        best_n      = 0

        # Per-regime tracking: {regime: (best_score, best_params)}
        regime_best: dict[str, tuple[float, dict]] = {}

        spread_r  = SPREAD_COST_R_SYMBOL.get(symbol, SPREAD_COST_R.get(trading_type, 0.05))
        bt_window = _BT_WINDOW.get(trading_type, 500)

        # Build secondary-timeframe extras for strategies that need them.
        # The optimizer only fetches one df (primary TF). For multi-TF strategies
        # we reuse the same df as the secondary TF — it's a reasonable approximation
        # for parameter search (trend direction changes slowly relative to entry TF).
        _extra_dfs: dict = {}
        if strategy_name in ("macd_ema_trend", "rsi_divergence"):
            # Both strategies use df_h1 for trend confirmation; the optimizer
            # receives H1 data (TRADING_TYPE_TF["day_trading"] = "H1"), so
            # pass the same df as df_h1.
            _extra_dfs = {"df_h1": df}
        elif strategy_name == "ema_scalp":
            # ema_scalp wants M5 bias; optimizer provides M5 for scalping.
            _extra_dfs = {"df_m5": df}
        elif strategy_name == "ema_trend_rider":
            _extra_dfs = {"df_h4": df, "df_d1": df}
        elif strategy_name == "weekly_breakout":
            _extra_dfs = {"df_daily": df}

        for combo in combos:
            time.sleep(0)  # yield CPU between combos to prevent event-loop starvation
            wr, avg_rr, n, regime_stats = _backtest_combo(
                strategy_cls, dispatch, df, combo,
                cfg["step"], cfg["max_hold"], cfg["warmup"], spread_r, symbol,
                _extra_dfs, bt_window,
            )
            # Score = win_rate weighted by quality of avg R:R
            score = wr * max(avg_rr, 0.0) if n >= MIN_BACKTEST_SIGNALS else 0.0
            if score > best_score:
                best_score  = score
                best_params = combo
                best_n      = n

            # Track per-regime best combo
            for regime_label, rs in regime_stats.items():
                if rs["total"] >= max(MIN_BACKTEST_SIGNALS // 3, 5):
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
        try:
            if OPT_FILE.exists():
                return json.loads(OPT_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"Optimizer: could not load params file: {exc}")
        return {}

    def _save_params(
        self,
        strategy_name: str,
        symbol: str,
        params: dict,
        regime_params: dict[str, dict] | None = None,
    ) -> None:
        with self._lock:
            data = self._load_opt()
            data.setdefault(strategy_name, {})
            # Merge: keep existing by_regime if present, update with new findings
            existing = data[strategy_name].get(symbol, {})
            existing_by_regime = existing.get("by_regime", {}) if isinstance(existing, dict) else {}
            if regime_params:
                existing_by_regime.update(regime_params)
            entry = dict(params)
            if existing_by_regime:
                entry["by_regime"] = existing_by_regime
            data[strategy_name][symbol] = entry
            # Also update __global__ if this is the first symbol
            if "__global__" not in data[strategy_name]:
                data[strategy_name]["__global__"] = entry
            OPT_FILE.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )

    _STATUS_FILE = Path(__file__).parent / "data" / "optimizer_status.json"

    def _load_status(self) -> None:
        try:
            if self._STATUS_FILE.exists():
                self._status = json.loads(self._STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:
            self._status = {}

    def _save_status(self) -> None:
        try:
            self._STATUS_FILE.parent.mkdir(exist_ok=True)
            self._STATUS_FILE.write_text(
                json.dumps(self._status, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"Optimizer: could not save status: {exc}")


# Application singleton
optimizer = ParamOptimizer()
