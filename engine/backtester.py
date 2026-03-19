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
)

# Natural backtest timeframe per trading type (matches LSTM training TF)
BT_TIMEFRAME: dict[str, str] = {
    "scalping":    "M5",
    "day_trading": "H1",
    "swing":       "H4",
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
            if low  <= sl: return "sl_hit", sl, j
            if high >= tp: return "tp_hit", tp, j
        else:
            if high >= sl: return "sl_hit", sl, j
            if low  <= tp: return "tp_hit", tp, j

    # Max hold reached — exit at close of last bar
    exit_price = float(df.iloc[end - 1]["close"])
    return "timeout", exit_price, end - 1


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(
    strategy_name: str,
    symbol: str,
    df: pd.DataFrame,
    trading_type: str,
    initial_balance: float = 10_000.0,
    risk_pct: float = 1.0,
) -> Optional[BacktestResult]:
    """
    Walk-forward backtest a strategy on historical OHLCV data.

    At each bar the strategy is evaluated on all preceding bars. If a signal
    with a valid SL/TP fires, the trade is simulated. After the trade closes
    the walk resumes after the exit bar.

    Args:
        strategy_name:   key from PARAM_GRIDS / _DISPATCH (e.g. "ema_scalp")
        symbol:          MT5 symbol string
        df:              OHLCV DataFrame, sorted oldest→newest
        trading_type:    "scalping" | "day_trading" | "swing"
        initial_balance: starting equity for P&L calculations
        risk_pct:        percent of current equity risked per trade (e.g. 1.0 = 1%)

    Returns:
        BacktestResult or None if strategy_name is unknown.
    """
    strat_map    = _get_strategy_map()
    strategy_cls = strat_map.get(strategy_name)
    if strategy_cls is None:
        logger.warning(f"Backtester: unknown strategy {strategy_name!r}")
        return None

    dispatch = _DISPATCH.get(strategy_name, lambda s, d: s.calculate(d))
    cfg      = _BACKTEST_CONFIG.get(trading_type, _BACKTEST_CONFIG["day_trading"])
    step     = cfg["step"]
    max_hold = cfg["max_hold"]
    warmup   = cfg["warmup"]
    tf_label = BT_TIMEFRAME.get(trading_type, "H1")

    trades: list[BacktestTrade] = []
    equity   = initial_balance
    i        = warmup
    trade_num = 0

    while i < len(df) - 1:
        # Run strategy on bars 0..i
        try:
            strat  = strategy_cls(symbol=symbol, params={})
            result = dispatch(strat, df.iloc[: i + 1])
            sig    = result.signal
        except Exception as exc:
            logger.debug(f"Backtester signal error at bar {i}: {exc}")
            i += step
            continue

        if not sig or sig.direction not in ("BUY", "SELL") or not sig.tp_price:
            i += step
            continue

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

        # P&L as % of equity: risk_pct% is the downside; scale gain by actual RR
        if outcome == "tp_hit":
            tp_rr    = abs((sig.tp_price - sig.entry_price)) / risk
            pnl_pct  = risk_pct * tp_rr
        elif outcome == "sl_hit":
            pnl_pct  = -risk_pct
        else:   # timeout
            pnl_pct  = risk_pct * rr

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
    losses = [abs(p) for p in pnls if p <= 0]

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

    # Annualised Sharpe
    arr    = np.array(pnls)
    sharpe = float(
        (arr.mean() / (arr.std() + 1e-9)) * np.sqrt(252)
        if n > 1 else 0.0
    )

    total_pnl_pct = (equity - initial_balance) / initial_balance * 100.0

    equity_curve = [{"time": t.exit_time, "equity": t.equity} for t in trades]

    avg_win  = gross_win  / max(len(wins),   1)
    avg_loss = gross_loss / max(len(losses), 1)

    return BacktestResult(
        symbol            = symbol,
        strategy          = strategy_name,
        trading_type      = trading_type,
        timeframe         = tf_label,
        bars_tested       = len(df),
        initial_balance   = initial_balance,
        trades            = trades,
        equity_curve      = equity_curve,
        total_trades      = n,
        win_rate          = round(win_rate, 4),
        profit_factor     = round(profit_factor, 3),
        max_drawdown_pct  = round(max_dd, 2),
        sharpe_ratio      = round(sharpe, 3),
        total_pnl_pct     = round(total_pnl_pct, 2),
        avg_rr            = round(float(np.mean([t.rr for t in trades])), 3),
        best_trade_pct    = round(max(pnls), 4),
        worst_trade_pct   = round(min(pnls), 4),
        avg_trade_pct     = round(float(np.mean(pnls)), 4),
        expectancy_pct    = round(
            win_rate * avg_win - (1.0 - win_rate) * avg_loss, 4
        ),
    )
