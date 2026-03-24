"""
Analytics routes — performance metrics for live and paper accounts,
segmented by trading mode (scalping / day_trading / swing).

Endpoints:
    GET /analytics/performance
        ?account=paper|live|all
        &trading_type=scalping|day_trading|swing  (optional)
        &limit=5000

Returns a comprehensive performance object including:
    - Summary stats (win_rate, total_profit, avg_rr, avg_confidence)
    - Sharpe ratio (annualised, daily-return basis)
    - Sortino ratio (annualised, downside-deviation basis)
    - Maximum drawdown % (peak-to-trough on equity curve)
    - Equity curve (cumulative P&L time series)
    - Per-strategy breakdown
    - Per-symbol breakdown
    - Trade quality distribution (high/medium/low)
    - Failure analysis (worst symbols, worst strategies, streak, hour-of-day)
    - Per-mode breakdown (scalping / day_trading / swing)
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Optional

import numpy as np
from fastapi import APIRouter, Query

router = APIRouter()


# ── helpers ──────────────────────────────────────────────────────────────────

def _load_closed(
    account: str,
    trading_type: Optional[str],
    limit: int,
) -> list[dict]:
    """Return closed trade entries from the journal, filtered by account + mode."""
    from engine.trade_journal import trade_journal
    entries = trade_journal.get(
        account=account,
        trading_type=trading_type if trading_type != "all" else None,
        event="close",
        limit=limit,
    )
    return [e for e in entries if e.get("profit") is not None]


def _daily_returns(closed: list[dict]) -> list[float]:
    """Aggregate P&L per calendar day (UTC), return list of daily dollar returns."""
    daily: dict[str, float] = defaultdict(float)
    for e in closed:
        ts = e.get("close_time") or e.get("logged_at") or ""
        day = ts[:10] if len(ts) >= 10 else "unknown"
        daily[day] += float(e["profit"] or 0)
    return list(daily.values())


def _sharpe(daily_rets: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sharpe ratio from daily returns."""
    if len(daily_rets) < 2:
        return 0.0
    arr = np.array(daily_rets) - risk_free_daily
    std = float(np.std(arr, ddof=1))
    if std == 0:
        return 0.0
    return float(np.mean(arr) / std * math.sqrt(252))


def _sortino(daily_rets: list[float], risk_free_daily: float = 0.0) -> float:
    """Annualised Sortino ratio (downside deviation only)."""
    if len(daily_rets) < 2:
        return 0.0
    arr = np.array(daily_rets) - risk_free_daily
    downside = arr[arr < 0]
    if len(downside) == 0:
        return 0.0  # no losing days — undefined, return 0 (not inf which breaks JSON)
    down_std = float(np.std(downside, ddof=1))
    if down_std == 0:
        return 0.0
    return round(float(np.mean(arr) / down_std * math.sqrt(252)), 3)


def _max_drawdown(equity_curve: list[float]) -> float:
    """Maximum peak-to-trough drawdown as a percentage of peak equity."""
    if len(equity_curve) < 2:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)


def _equity_curve(closed: list[dict]) -> list[dict]:
    """Build an equity curve as list of {time, equity} sorted by close_time."""
    sorted_trades = sorted(
        closed,
        key=lambda e: e.get("close_time") or e.get("logged_at") or "",
    )
    cumulative = 0.0
    curve = []
    for e in sorted_trades:
        cumulative += float(e["profit"] or 0)
        ts = e.get("close_time") or e.get("logged_at") or ""
        curve.append({"time": ts, "equity": round(cumulative, 2)})
    return curve


def _losing_streak(closed: list[dict]) -> tuple[int, int]:
    """Return (max_consecutive_losses, current_streak)."""
    sorted_trades = sorted(
        closed,
        key=lambda e: e.get("close_time") or e.get("logged_at") or "",
    )
    max_streak = 0
    cur_streak  = 0
    for e in sorted_trades:
        if (e["profit"] or 0) <= 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0
    return max_streak, cur_streak


def _by_bucket(
    closed: list[dict],
    key_fn,
) -> dict[str, dict]:
    """Group closed trades by a key function, return per-bucket stats."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for e in closed:
        buckets[key_fn(e)].append(e)
    result = {}
    for label, trades in buckets.items():
        total  = len(trades)
        wins   = sum(1 for t in trades if (t["profit"] or 0) > 0)
        profit = sum(float(t["profit"] or 0) for t in trades)
        rrs    = []
        for t in trades:
            risk   = abs((t.get("entry") or 0) - (t.get("sl") or 0))
            reward = abs(float(t.get("profit") or 0))
            if risk > 0 and t.get("entry"):
                rrs.append(reward / (risk * (t.get("volume") or 1) * 1))
        result[label] = {
            "total":       total,
            "wins":        wins,
            "losses":      total - wins,
            "win_rate":    round(wins / total * 100, 1) if total else 0.0,
            "total_profit": round(profit, 2),
            "avg_profit":  round(profit / total, 2) if total else 0.0,
        }
    return result


def _hour_of_day(closed: list[dict]) -> dict[str, dict]:
    """Win rate by UTC close hour (0-23)."""
    def _hour(e: dict) -> str:
        ts = e.get("close_time") or e.get("logged_at") or ""
        if len(ts) >= 13:
            try:
                return str(int(ts[11:13]))
            except ValueError:
                pass
        return "unknown"
    return _by_bucket(closed, _hour)


def _trade_quality_distribution(closed: list[dict]) -> dict[str, int]:
    """Categorise trades by stored confidence score (from comment field fallback)."""
    high = medium = low = 0
    for e in closed:
        # confidence may be embedded in comment as JSON or separate field
        conf_val = None
        raw_comment = e.get("comment", "")
        # try to find confidence in comment e.g. "day_trading|macd_ema_trend|conf=0.72"
        for part in str(raw_comment).split("|"):
            if part.startswith("conf="):
                try:
                    conf_val = float(part[5:])
                except ValueError:
                    pass
        if conf_val is None:
            # unknown confidence — count as medium
            medium += 1
        elif conf_val >= 0.75:
            high += 1
        elif conf_val >= 0.50:
            medium += 1
        else:
            low += 1
    return {"high": high, "medium": medium, "low": low}


# ── endpoint ─────────────────────────────────────────────────────────────────

@router.get("/performance")
def get_performance(
    account:      str = Query("all",  pattern="^(paper|live|all)$"),
    trading_type: str = Query("all",  pattern="^(scalping|day_trading|swing|all)$"),
    limit:        int = Query(5000,   ge=1, le=50_000),
):
    """
    Return comprehensive trading performance analytics.

    Query params:
        account      — paper | live | all
        trading_type — scalping | day_trading | swing | all
        limit        — max closed trades to analyse (default 5000)
    """
    tt_filter = None if trading_type == "all" else trading_type
    closed    = _load_closed(account, tt_filter, limit)

    if not closed:
        return {
            "account":       account,
            "trading_type":  trading_type,
            "total_trades":  0,
            "message":       "No closed trades found for the selected filters.",
        }

    total  = len(closed)
    wins   = [e for e in closed if (e["profit"] or 0) > 0]
    losses = [e for e in closed if (e["profit"] or 0) <= 0]

    total_profit  = round(sum(float(e["profit"] or 0) for e in closed), 2)
    avg_profit    = round(total_profit / total, 2)
    avg_win       = round(sum(float(e["profit"] or 0) for e in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss      = round(sum(float(e["profit"] or 0) for e in losses) / len(losses), 2) if losses else 0.0

    daily_rets    = _daily_returns(closed)
    curve_points  = _equity_curve(closed)
    equity_values = [p["equity"] for p in curve_points]

    max_dd        = _max_drawdown(equity_values)
    sharpe        = round(_sharpe(daily_rets), 3)
    sortino       = round(_sortino(daily_rets), 3)
    max_streak, cur_streak = _losing_streak(closed)

    # Per-strategy breakdown
    by_strategy = _by_bucket(closed, lambda e: e.get("comment", "").split("|")[1]
                              if "|" in e.get("comment", "") else e.get("comment", "unknown"))

    # Per-symbol breakdown
    by_symbol = _by_bucket(closed, lambda e: e.get("symbol", "unknown"))

    # Per trading-type breakdown (useful when account=all&trading_type=all)
    by_mode = _by_bucket(closed, lambda e: e.get("trading_type", "unknown"))

    # Per account breakdown (useful when account=all)
    by_account = _by_bucket(closed, lambda e: e.get("account_mode", "unknown"))

    # Hour-of-day win rate
    by_hour = _hour_of_day(closed)

    # Trade quality distribution
    quality_dist = _trade_quality_distribution(closed)

    # Failure analysis: worst 5 symbols and strategies by total loss
    worst_symbols = sorted(
        by_symbol.items(),
        key=lambda kv: kv[1]["total_profit"],
    )[:5]
    worst_strategies = sorted(
        by_strategy.items(),
        key=lambda kv: kv[1]["total_profit"],
    )[:5]

    # Average RR from entry/sl/tp fields
    rr_vals = []
    for e in closed:
        entry = e.get("entry") or 0
        sl    = e.get("sl") or 0
        tp    = e.get("tp") or 0
        if entry and sl and tp and abs(entry - sl) > 0:
            rr_vals.append(abs(tp - entry) / abs(entry - sl))
    avg_rr = round(float(np.mean(rr_vals)), 3) if rr_vals else 0.0

    return {
        "account":          account,
        "trading_type":     trading_type,
        "total_trades":     total,
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / total * 100, 1),
        "total_profit":     total_profit,
        "avg_profit":       avg_profit,
        "avg_win":          avg_win,
        "avg_loss":         avg_loss,
        "avg_rr":           avg_rr,
        "sharpe_ratio":     sharpe,
        "sortino_ratio":    sortino,
        "max_drawdown_pct": max_dd,
        "max_losing_streak": max_streak,
        "current_losing_streak": cur_streak,
        "equity_curve":     curve_points,
        "by_strategy":      by_strategy,
        "by_symbol":        by_symbol,
        "by_mode":          by_mode,
        "by_account":       by_account,
        "by_hour":          by_hour,
        "trade_quality":    quality_dist,
        "failure_analysis": {
            "worst_symbols":    [{"symbol": k, **v} for k, v in worst_symbols],
            "worst_strategies": [{"strategy": k, **v} for k, v in worst_strategies],
            "max_losing_streak": max_streak,
            "current_losing_streak": cur_streak,
        },
    }


@router.get("/regime/status")
def get_regime_status():
    """Return the current market regime label for all classified symbols."""
    try:
        from engine.regime_classifier import regime_classifier
        return {"regimes": regime_classifier.all_labels()}
    except Exception as exc:
        return {"regimes": {}, "error": str(exc)}
