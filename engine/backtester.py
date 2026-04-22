"""
Backtester — user-facing strategy simulation on historical OHLCV data.

Walk-forward approach: at each bar the strategy is evaluated on all prior bars;
if a signal fires the trade is simulated bar-by-bar until SL, TP, or max-hold
timeout is reached.

Returns a BacktestResult with:
  - every simulated trade (entry/exit time, prices, outcome, R-multiple, P&L %)
  - equity curve (snapshot after each closed trade)
  - summary metrics (win rate, profit factor, max drawdown, Sharpe, etc.)

Usage:
    from engine.backtester import run_backtest, BT_TIMEFRAME
    result = run_backtest("ema_scalp", "EURUSD", df, "scalping")
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

from ai.param_optimizer import (
    _BACKTEST_CONFIG,
    _DISPATCH,
    _get_strategy_map,
    SPREAD_COST_R,
    SPREAD_COST_R_SYMBOL,
)

# Natural backtest timeframe per trading type (matches LSTM training TF)
BT_TIMEFRAME: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
}

# Per-strategy primary TF override (strategies whose entry TF differs from BT_TIMEFRAME)
BT_STRATEGY_TIMEFRAME: dict[str, str] = {
    "ema_scalp":       "M5",   # entry on M5, trend on M15
    "macd_ema_trend":  "M15",  # entry on M15, trend on H1
    "rsi_divergence":  "M30",  # entry on M30, trend on H1
    "ema_trend_rider": "H1",   # entry on H1, bias on H4 + D1
    "weekly_breakout": "H4",   # entry on H4, bias on D1
}

# Secondary timeframes required per strategy {kwarg_name: timeframe_string}
BT_EXTRA_TIMEFRAMES: dict[str, dict[str, str]] = {
    "ema_scalp":       {"df_m15":   "M15"},
    "macd_ema_trend":  {"df_h1":    "H1"},
    "rsi_divergence":  {"df_h1":    "H1"},
    "ema_trend_rider": {"df_h4":    "H4", "df_d1": "D1"},
    "weekly_breakout": {"df_daily": "D1"},
}


# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    trade_num:    int
    entry_time:   str
    exit_time:    str
    direction:    str    # BUY | SELL
    entry_price:  float
    exit_price:   float
    sl_price:     float
    tp_price:     float
    outcome:      str    # tp_hit | sl_hit | timeout
    rr:           float  # R-multiple achieved (positive = profitable)
    pnl_pct:      float  # % of equity at entry (positive = gain)
    equity:       float  # running equity after this trade closes
    conf_score:   float = 0.0  # AI confidence score (0–1); 0.0 when filters disabled


@dataclass
class BacktestResult:
    symbol:           str
    strategy:         str
    trading_type:     str
    timeframe:        str
    bars_tested:      int
    initial_balance:  float
    trades:           list[BacktestTrade]
    equity_curve:     list[dict]   # [{time, equity}] — one point per closed trade

    # Summary metrics
    total_trades:     int
    win_rate:         float   # 0–1
    profit_factor:    float
    max_drawdown_pct: float   # worst peak-to-trough in %
    sharpe_ratio:     float   # annualised
    total_pnl_pct:    float   # total % return from initial_balance
    avg_rr:           float
    best_trade_pct:   float
    worst_trade_pct:  float
    avg_trade_pct:    float
    expectancy_pct:   float  # expected return per trade
    ai_filters_applied: bool  = False  # True when LSTM+RL scored and gated every trade
    avg_confidence:     float = 0.0   # mean AI score across accepted trades (0 if disabled)

    def to_dict(self) -> dict:
        return asdict(self)


# ── Core simulation helpers ───────────────────────────────────────────────────

def _sim_trade(
    df: pd.DataFrame,
    idx: int,
    direction: str,
    entry: float,
    sl: float,
    tp: float,
    max_hold: int,
) -> tuple[str, float, int]:
    """Walk bars forward from idx+1. Returns (outcome, exit_price, exit_bar_idx)."""
    if not (sl and tp and entry):
        return "invalid", entry, idx

    end = min(idx + 1 + max_hold, len(df))
    for j in range(idx + 1, end):
        high = float(df.iloc[j]["high"])
        low  = float(df.iloc[j]["low"])
        if direction == "BUY":
            sl_touched = low  <= sl
            tp_touched = high >= tp
            # When both SL and TP are breached in the same bar (e.g. large news candle)
            # use the conservative assumption: SL was hit first.  The previous code
            # checked TP first (optimistic) which inflated backtest win rates.
            if sl_touched and tp_touched:
                return "sl_hit", sl, j
            if tp_touched: return "tp_hit", tp, j
            if sl_touched: return "sl_hit", sl, j
        else:
            sl_touched = high >= sl
            tp_touched = low  <= tp
            if sl_touched and tp_touched:
                return "sl_hit", sl, j
            if tp_touched: return "tp_hit", tp, j
            if sl_touched: return "sl_hit", sl, j

    # Max hold reached — check if the bar's close is beyond SL before calling timeout.
    # A gap/news candle in the last bar could close well past the SL; if we return
    # "timeout" the caller uses the close price, overstating the actual exit level.
    exit_price = float(df.iloc[end - 1]["close"])
    if direction == "BUY" and exit_price <= sl:
        return "sl_hit", sl, end - 1
    if direction == "SELL" and exit_price >= sl:
        return "sl_hit", sl, end - 1
    return "timeout", exit_price, end - 1


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(
    strategy_name: str,
    symbol: str,
    df: pd.DataFrame,
    trading_type: str,
    initial_balance: float = 10_000.0,
    risk_pct: float = 1.0,
    extra_dfs: dict[str, pd.DataFrame] | None = None,
    use_ai_filters: bool = True,
    commission_r: float = 0.05,
) -> Optional[BacktestResult]:
    """
    Walk-forward backtest a strategy on historical OHLCV data.

    At each bar the strategy is evaluated on all preceding bars. If a signal
    with a valid SL/TP fires, the trade is simulated. After the trade closes
    the walk resumes after the exit bar.

    Args:
        strategy_name:   key from PARAM_GRIDS / _DISPATCH (e.g. "ema_scalp")
        symbol:          MT5 symbol string
        df:              OHLCV DataFrame (primary TF), sorted oldest→newest
        trading_type:    "scalping" | "day_trading" | "swing"
        initial_balance: starting equity for P&L calculations
        risk_pct:        percent of current equity risked per trade (e.g. 1.0 = 1%)
        extra_dfs:       secondary TF dataframes keyed by strategy kwarg name,
                         e.g. {"df_h1": h1_df} for macd_ema_trend
        use_ai_filters:  when True, apply LSTM confidence scoring and RL agent gate
                         to every signal — matching live execution conditions.
                         Low-confidence or RL-rejected signals are skipped.
                         The RL risk_factor also scales per-trade P&L as in live.
        commission_r:    round-turn commission expressed in units of R (default 0.05).
                         Deducted on every closed trade regardless of outcome.
                         0.05 ≈ 5% of the 1R SL distance → ~$5 commission per
                         $100 risked, representative of XM Standard (~$7/lot avg).
                         Set to 0.0 to disable commission modelling.

    Returns:
        BacktestResult or None if strategy_name is unknown.
    """
    strat_map    = _get_strategy_map()
    strategy_cls = strat_map.get(strategy_name)
    if strategy_cls is None:
        logger.warning(f"Backtester: unknown strategy {strategy_name!r}")
        return None

    dispatch = _DISPATCH.get(strategy_name, lambda s, d, e={}: s.calculate(d))
    cfg      = _BACKTEST_CONFIG.get(trading_type, _BACKTEST_CONFIG["day_trading"])
    spread_r = SPREAD_COST_R_SYMBOL.get(symbol, SPREAD_COST_R.get(trading_type, 0.05))
    step     = cfg["step"]
    max_hold = cfg["max_hold"]
    warmup   = cfg["warmup"]
    tf_label = BT_STRATEGY_TIMEFRAME.get(strategy_name) or BT_TIMEFRAME.get(trading_type, "H1")

    _extra_dfs: dict[str, pd.DataFrame] = extra_dfs or {}
    # Determine if primary df has a parseable time column for lookahead-safe slicing
    _has_time = "time" in df.columns

    # Rolling window size: strategies only need the last N bars for indicator look-back.
    # Using a fixed window instead of df.iloc[:i+1] keeps each bar O(window) instead
    # of O(i), turning the overall loop from O(n²) to O(n).
    SIGNAL_WINDOW = 350

    trades: list[BacktestTrade] = []
    equity   = initial_balance
    i        = warmup
    trade_num = 0

    try:
        from ai.param_optimizer import optimizer as _opt
        _strat_params: dict = _opt.get_params(strategy_name, symbol) or {}
    except Exception:
        _strat_params = {}

    # Lazy-load AI components once outside the loop for efficiency
    _scorer     = None
    _rl_manager = None
    if use_ai_filters:
        try:
            from ai.signal_scorer import scorer as _scorer
            from ai.rl_agent import rl_manager as _rl_manager
        except Exception as exc:
            logger.warning(f"Backtester: AI imports failed, filters disabled: {exc}")
            _scorer = None
            _rl_manager = None

    while i < len(df) - 1:
        # Slice secondary dataframes up to (and including) the current bar time
        # to prevent lookahead bias.
        # Rolling window: only pass the last SIGNAL_WINDOW bars to the strategy.
        window_start = max(0, i + 1 - SIGNAL_WINDOW)
        df_window    = df.iloc[window_start : i + 1]

        if _extra_dfs and _has_time:
            bar_time = df.iloc[i]["time"]
            sliced_extra = {
                k: (v[v["time"] < bar_time].iloc[-SIGNAL_WINDOW:] if "time" in v.columns
                    else v.iloc[-SIGNAL_WINDOW:])
                for k, v in _extra_dfs.items()
            }
        else:
            sliced_extra = {k: v.iloc[-SIGNAL_WINDOW:] for k, v in _extra_dfs.items()} if _extra_dfs else _extra_dfs

        # Run strategy on windowed slice
        try:
            strat  = strategy_cls(symbol=symbol, params=_strat_params)
            result = dispatch(strat, df_window, sliced_extra)
            sig    = result.signal
        except Exception as exc:
            logger.debug(f"Backtester signal error at bar {i}: {exc}")
            i += step
            continue

        if not sig or sig.direction not in ("BUY", "SELL") or not sig.tp_price:
            i += step
            continue

        # ── AI filters (LSTM confidence + RL gate) ────────────────────────
        _conf_score    = 0.0
        _ai_risk_scale = 1.0
        if _scorer is not None:
            try:
                _conf_score = _scorer.score(
                    symbol, sig.direction,
                    sig.entry_price, sig.sl_price, sig.tp_price,
                    df_window, trading_type,
                )
                # RL gate: skip signal if confidence is below learned threshold
                if _rl_manager is not None and not _scorer.is_tradeable(_conf_score, trading_type, strategy_name):
                    i += step
                    continue
                # RL risk factor: scale position size as the live engine does
                if _rl_manager is not None:
                    _ai_risk_scale = _rl_manager.risk_factor(strategy_name, trading_type)
            except Exception as exc:
                logger.debug(f"Backtester AI filter error at bar {i}: {exc}")

        outcome, exit_price, exit_bar = _sim_trade(
            df, i,
            sig.direction, sig.entry_price, sig.sl_price, sig.tp_price,
            max_hold,
        )

        if outcome == "invalid":
            i += step
            continue

        risk = abs(sig.entry_price - sig.sl_price)
        if risk == 0:
            i += step
            continue

        raw_move = (
            (exit_price - sig.entry_price)
            if sig.direction == "BUY"
            else (sig.entry_price - exit_price)
        )
        rr = raw_move / risk

        # P&L as % of equity: risk_pct% is the downside; scale gain by actual RR.
        # Deduct spread cost (same as param_optimizer) so results are not overly optimistic.
        # Apply RL risk_factor scaling so undersized positions match live execution.
        effective_risk = risk_pct * _ai_risk_scale
        if outcome == "tp_hit":
            tp_rr    = abs((sig.tp_price - sig.entry_price)) / risk
            pnl_pct  = effective_risk * (tp_rr - spread_r) - effective_risk * commission_r
        elif outcome == "sl_hit":
            pnl_pct  = -effective_risk * (1.0 + spread_r) - effective_risk * commission_r
        else:   # timeout
            pnl_pct  = effective_risk * (rr - spread_r) - effective_risk * commission_r

        equity    *= 1.0 + pnl_pct / 100.0
        trade_num += 1

        entry_time = str(df.iloc[i]["time"])        if "time" in df.columns else ""
        exit_time  = str(df.iloc[exit_bar]["time"]) if "time" in df.columns else ""

        trades.append(BacktestTrade(
            trade_num   = trade_num,
            entry_time  = entry_time,
            exit_time   = exit_time,
            direction   = sig.direction,
            entry_price = round(float(sig.entry_price), 6),
            exit_price  = round(float(exit_price),       6),
            sl_price    = round(float(sig.sl_price),      6),
            conf_score  = round(_conf_score, 4),
            tp_price    = round(float(sig.tp_price or 0), 6),
            outcome     = outcome,
            rr          = round(rr, 3),
            pnl_pct     = round(pnl_pct, 4),
            equity      = round(equity, 2),
        ))

        # Skip past the closed trade to avoid reusing the same bars
        i = exit_bar + max(step, 1)

    # ── Summary metrics ──────────────────────────────────────────────────
    n = len(trades)

    if n == 0:
        return BacktestResult(
            symbol=symbol, strategy=strategy_name, trading_type=trading_type,
            timeframe=tf_label, bars_tested=len(df), initial_balance=initial_balance,
            trades=[], equity_curve=[],
            total_trades=0, win_rate=0.0, profit_factor=0.0,
            max_drawdown_pct=0.0, sharpe_ratio=0.0, total_pnl_pct=0.0,
            avg_rr=0.0, best_trade_pct=0.0, worst_trade_pct=0.0,
            avg_trade_pct=0.0, expectancy_pct=0.0,
        )

    pnls   = [t.pnl_pct for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]   # LOGIC-BT-1: was <= 0; break-even trades excluded

    win_rate      = len(wins) / n
    gross_win     = sum(wins)   if wins   else 0.0
    gross_loss    = sum(losses) if losses else 1e-9
    profit_factor = gross_win / gross_loss

    # Max drawdown
    eq_vals = [initial_balance] + [t.equity for t in trades]
    peak = eq_vals[0]
    max_dd = 0.0
    for eq in eq_vals:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100.0
        if dd > max_dd:
            max_dd = dd

    # Annualised Sharpe — factor per timeframe so H4/D1 strategies aren't unfairly penalised
    _ANN_FACTOR: dict[str, float] = {
        "M1": 252 * 1440, "M5": 252 * 288, "M15": 252 * 96,
        "M30": 252 * 48, "H1": 252 * 24, "H4": 252 * 6,
        "D1": 252, "W1": 52,
    }
    _ann = _ANN_FACTOR.get(tf_label, 252)
    arr    = np.array(pnls)
    sharpe = float(
        (arr.mean() / (arr.std() + 1e-9)) * np.sqrt(_ann)
        if n > 1 else 0.0
    )

    total_pnl_pct = (equity - initial_balance) / initial_balance * 100.0

    equity_curve = [{"time": t.exit_time, "equity": t.equity} for t in trades]

    avg_win  = gross_win  / max(len(wins),   1)
    avg_loss = gross_loss / max(len(losses), 1)

    _ai_filters_on = use_ai_filters and _scorer is not None
    _scored = [t.conf_score for t in trades if t.conf_score > 0.0]
    _avg_conf = round(float(np.mean(_scored)), 4) if _scored else 0.0

    return BacktestResult(
        symbol              = symbol,
        strategy            = strategy_name,
        trading_type        = trading_type,
        timeframe           = tf_label,
        bars_tested         = len(df),
        initial_balance     = initial_balance,
        trades              = trades,
        equity_curve        = equity_curve,
        total_trades        = n,
        win_rate            = round(win_rate, 4),
        profit_factor       = round(profit_factor, 3),
        max_drawdown_pct    = round(max_dd, 2),
        sharpe_ratio        = round(sharpe, 3),
        total_pnl_pct       = round(total_pnl_pct, 2),
        avg_rr              = round(float(np.mean([t.rr for t in trades])), 3),
        best_trade_pct      = round(max(pnls), 4),
        worst_trade_pct     = round(min(pnls), 4),
        avg_trade_pct       = round(float(np.mean(pnls)), 4),
        expectancy_pct      = round(
            win_rate * avg_win - (1.0 - win_rate) * avg_loss, 4
        ),
        ai_filters_applied  = _ai_filters_on,
        avg_confidence      = _avg_conf,
    )
