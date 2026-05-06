"""
Portfolio Optimization — Task 25

Optimizes capital allocation across multiple trading strategies to maximize
risk-adjusted returns while minimizing correlation.

Techniques:
1. Kelly Criterion — mathematically optimal position sizing based on win rate + avg R:R
2. Correlation Matrix — measure strategy correlation to avoid over-exposure
3. Risk Parity — allocate capital so each strategy contributes equal risk
4. Rebalance Suggestions — when to shift capital between strategies

Toggle: Disabled by default. Enable in config/app.json:
  {
    "portfolio_optimization": {
      "enabled": false,
      "min_trades_required": 30,  // per strategy before optimization kicks in
      "rebalance_threshold": 0.15  // trigger rebalance if allocation drift > 15%
    }
  }
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import APIRouter, Query
from loguru import logger

router = APIRouter()

_APP_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "app.json"


def _is_enabled() -> tuple[bool, dict]:
    """Check if portfolio optimization is enabled in config."""
    try:
        cfg = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        po_cfg = cfg.get("portfolio_optimization", {})
        enabled = bool(po_cfg.get("enabled", False))
        return enabled, po_cfg
    except Exception as e:
        logger.debug(f"Portfolio optimization config load failed: {e}")
        return False, {}


def _kelly_fraction(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    Calculate Kelly Criterion fraction for optimal position sizing.
    
    Formula: f = (p * b - q) / b
    Where:
      p = win probability
      q = loss probability (1 - p)
      b = avg_win / avg_loss (odds)
    
    Returns value between 0 and 1 (fraction of capital to risk per trade).
    Values > 1 or < 0 are clamped to [0, 1].
    """
    if win_rate <= 0 or win_rate >= 1 or avg_loss == 0:
        return 0.0
    
    p = win_rate
    q = 1 - p
    b = abs(avg_win / avg_loss)
    
    fraction = (p * b - q) / b
    
    # Clamp to [0, 1] and apply 50% Kelly (conservative)
    return max(0.0, min(1.0, fraction * 0.5))


def _correlation_matrix(strategy_returns: dict[str, dict[str, float]]) -> dict:
    """
    Calculate correlation matrix between strategy daily returns.

    Args:
        strategy_returns: {strategy_name: {date_str: daily_return}}

    Returns date-aligned correlation — only the days where BOTH strategies were
    active are used (inner join). Zero-padding would introduce fake flat days and
    bias every coefficient toward zero.

    Returns:
        {
            "matrix": {"strategy_a_strategy_b": correlation_coefficient},
            "avg_correlation": float
        }
    """
    strategies = list(strategy_returns.keys())
    if len(strategies) < 2:
        return {"matrix": {}, "avg_correlation": 0.0}

    matrix = {}
    correlations = []

    for i, strat_a in enumerate(strategies):
        for strat_b in strategies[i + 1:]:
            # Inner join on dates — only days both strategies traded
            common_dates = sorted(
                set(strategy_returns[strat_a].keys()) &
                set(strategy_returns[strat_b].keys())
            )
            if len(common_dates) < 5:  # need at least 5 common days for a meaningful correlation
                continue
            returns_a = np.array([strategy_returns[strat_a][d] for d in common_dates])
            returns_b = np.array([strategy_returns[strat_b][d] for d in common_dates])
            # std must be non-zero for corrcoef to be meaningful
            if returns_a.std() == 0 or returns_b.std() == 0:
                continue
            corr = float(np.corrcoef(returns_a, returns_b)[0, 1])
            if not math.isnan(corr) and not math.isinf(corr):
                matrix[(strat_a, strat_b)] = round(corr, 3)
                correlations.append(abs(corr))

    avg_corr = sum(correlations) / len(correlations) if correlations else 0.0

    return {
        "matrix": {f"{a}_{b}": v for (a, b), v in matrix.items()},
        "avg_correlation": round(avg_corr, 3),
    }


def _risk_parity_allocation(strategy_volatilities: dict[str, float]) -> dict[str, float]:
    """
    Calculate risk parity allocation (each strategy contributes equal risk).
    
    Args:
        strategy_volatilities: {strategy_name: daily_volatility}
    
    Returns:
        {strategy_name: allocation_fraction}
    """
    if not strategy_volatilities:
        return {}
    
    # Inverse volatility weighting
    inv_vols = {s: 1.0 / max(v, 0.001) for s, v in strategy_volatilities.items()}
    total_inv = sum(inv_vols.values())
    
    allocations = {s: round(inv / total_inv, 3) for s, inv in inv_vols.items()}
    
    return allocations


@router.get("/status")
def portfolio_optimization_status():
    """Return portfolio optimization config and status."""
    enabled, cfg = _is_enabled()
    return {
        "enabled": enabled,
        "min_trades_required": cfg.get("min_trades_required", 30),
        "rebalance_threshold": cfg.get("rebalance_threshold", 0.15),
        "message": "Portfolio optimization is active" if enabled else "Portfolio optimization disabled (enable in config/app.json)"
    }


@router.get("/optimal-allocation")
def get_optimal_allocation(
    account: str = Query("all", description="paper | live | all"),
    account_login: Optional[int] = Query(None, description="Filter by specific MT5 account login number"),
    min_trades: Optional[int] = None,
):
    """
    Calculate optimal capital allocation across strategies.
    
    Returns:
        - kelly_allocations: Kelly Criterion-based allocation per strategy
        - risk_parity_allocations: Equal risk contribution allocation
        - correlation_matrix: Strategy correlation coefficients
        - rebalance_needed: Whether current allocation is sub-optimal
    """
    enabled, cfg = _is_enabled()
    if not enabled:
        return {
            "enabled": False,
            "message": "Portfolio optimization is disabled. Enable in config/app.json under 'portfolio_optimization.enabled'"
        }
    
    min_trades_req = min_trades or cfg.get("min_trades_required", 30)
    rebalance_threshold = cfg.get("rebalance_threshold", 0.15)
    
    from engine.trade_journal import trade_journal
    
    # Get closed trades - use account_login if provided
    if account_login is not None:
        entries = trade_journal.get(
            account="all",
            event="close",
            limit=5000,
            account_login=account_login,
        )
    else:
        entries = trade_journal.get(
            account=account if account != "all" else "all",
            event="close",
            limit=5000,
        )
    
    if len(entries) < min_trades_req:
        return {
            "enabled": True,
            "message": f"Insufficient data: {len(entries)} trades < {min_trades_req} required",
            "total_trades": len(entries),
            "min_trades_required": min_trades_req,
        }
    
    # Group trades by strategy
    by_strategy = defaultdict(list)
    strategy_daily_returns = defaultdict(lambda: defaultdict(float))
    
    for entry in entries:
        strategy = entry.get("comment") or entry.get("strategy") or "unknown"
        profit = entry.get("profit")
        if profit is not None:
            by_strategy[strategy].append(entry)
            
            # Aggregate daily returns
            close_time = entry.get("close_time") or entry.get("logged_at") or ""
            day = close_time[:10] if len(close_time) >= 10 else "unknown"
            strategy_daily_returns[strategy][day] += float(profit)
    
    # Calculate Kelly allocations
    kelly_allocations = {}
    risk_parity_vols = {}
    
    for strategy, trades in by_strategy.items():
        if len(trades) < min_trades_req:
            continue
        
        wins = [t for t in trades if t.get("profit", 0) > 0]
        losses = [t for t in trades if t.get("profit", 0) < 0]
        
        if not wins or not losses:
            continue
        
        win_rate = len(wins) / len(trades)
        avg_win = sum(t.get("profit", 0) for t in wins) / len(wins)
        avg_loss = abs(sum(t.get("profit", 0) for t in losses) / len(losses))
        
        kelly_frac = _kelly_fraction(win_rate, avg_win, avg_loss)
        kelly_allocations[strategy] = round(kelly_frac, 3)
        
        # Calculate volatility for risk parity
        daily_rets = list(strategy_daily_returns[strategy].values())
        if len(daily_rets) > 1:
            volatility = float(np.std(daily_rets, ddof=1))
            risk_parity_vols[strategy] = volatility
    
    # Normalize Kelly allocations to sum to 1.0
    total_kelly = sum(kelly_allocations.values())
    if total_kelly > 0:
        kelly_allocations = {s: round(v / total_kelly, 3) for s, v in kelly_allocations.items()}
    
    # Calculate risk parity allocations
    risk_parity_allocations = _risk_parity_allocation(risk_parity_vols)
    
    # Calculate correlation matrix
    strategy_returns_dicts = {
        s: dict(strategy_daily_returns[s])
        for s in kelly_allocations.keys()
    }
    correlation_data = _correlation_matrix(strategy_returns_dicts)
    
    # Check if rebalance needed
    equal_weight = 1.0 / len(kelly_allocations) if kelly_allocations else 0.0
    max_deviation = max(
        abs(v - equal_weight) for v in kelly_allocations.values()
    ) if kelly_allocations else 0.0
    
    rebalance_needed = max_deviation > rebalance_threshold
    
    return {
        "enabled": True,
        "total_trades": len(entries),
        "strategies_analyzed": len(kelly_allocations),
        "kelly_allocations": kelly_allocations,
        "risk_parity_allocations": risk_parity_allocations,
        "correlation_matrix": correlation_data["matrix"],
        "avg_strategy_correlation": correlation_data["avg_correlation"],
        "rebalance_needed": rebalance_needed,
        "rebalance_threshold": rebalance_threshold,
        "max_deviation_from_equal_weight": round(max_deviation, 3),
        "recommendation": (
            "High correlation detected — consider reducing exposure to correlated strategies"
            if correlation_data["avg_correlation"] > 0.7
            else "Low correlation — strategies are well diversified"
        ),
    }