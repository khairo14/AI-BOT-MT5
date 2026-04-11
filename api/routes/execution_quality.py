"""
Execution Quality Metrics — Task 26

Tracks order execution performance:
- Slippage: difference between expected price (signal) and actual fill price
- Fill Rate: percentage of orders successfully filled vs rejected
- Execution Time: milliseconds from order submission to fill

Endpoints:
    GET /execution-quality/metrics
        ?account=paper|live|all
        &trading_type=scalping|day_trading|swing  (optional)
        &limit=1000
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/metrics")
def get_execution_quality(
    account: str = Query("all", regex="^(paper|live|all)$"),
    trading_type: Optional[str] = Query(None, regex="^(scalping|day_trading|swing)$"),
    limit: int = Query(1000, ge=1, le=10000),
):
    """
    Return execution quality metrics for filled orders.
    
    Returns:
        - avg_slippage: average slippage in price units (per symbol)
        - avg_execution_time_ms: average execution time in milliseconds
        - fill_rate: % of orders filled successfully (requires tracking rejections)
        - by_symbol: breakdown of metrics per symbol
        - by_trading_type: breakdown by scalping/day_trading/swing
    """
    from engine.trade_journal import trade_journal

    # Get all open events (which contain execution quality metrics)
    entries = trade_journal.get(
        account=account if account != "all" else "all",
        trading_type=trading_type,
        event="open",
        limit=limit,
    )

    if not entries:
        return {
            "total_filled": 0,
            "avg_slippage": 0.0,
            "avg_execution_time_ms": 0.0,
            "by_symbol": {},
            "by_trading_type": {},
            "message": "No trades with execution data"
        }

    # Aggregate metrics
    total_filled = 0
    total_slippage = 0.0
    total_exec_time = 0.0
    slippage_count = 0
    exec_time_count = 0

    by_symbol = defaultdict(lambda: {
        "count": 0,
        "total_slippage": 0.0,
        "slippage_count": 0,
        "total_exec_time": 0.0,
        "exec_time_count": 0,
    })

    by_type = defaultdict(lambda: {
        "count": 0,
        "total_slippage": 0.0,
        "slippage_count": 0,
        "total_exec_time": 0.0,
        "exec_time_count": 0,
    })

    for entry in entries:
        total_filled += 1
        symbol = entry.get("symbol", "UNKNOWN")
        tt = entry.get("trading_type", "unknown")

        # Slippage tracking
        slippage = entry.get("slippage")
        if slippage is not None and slippage > 0:
            total_slippage += slippage
            slippage_count += 1
            by_symbol[symbol]["total_slippage"] += slippage
            by_symbol[symbol]["slippage_count"] += 1
            by_type[tt]["total_slippage"] += slippage
            by_type[tt]["slippage_count"] += 1

        # Execution time tracking
        exec_time = entry.get("execution_time_ms")
        if exec_time is not None and exec_time > 0:
            total_exec_time += exec_time
            exec_time_count += 1
            by_symbol[symbol]["total_exec_time"] += exec_time
            by_symbol[symbol]["exec_time_count"] += 1
            by_type[tt]["total_exec_time"] += exec_time
            by_type[tt]["exec_time_count"] += 1

        by_symbol[symbol]["count"] += 1
        by_type[tt]["count"] += 1

    # Calculate averages
    avg_slippage = total_slippage / slippage_count if slippage_count > 0 else 0.0
    avg_exec_time = total_exec_time / exec_time_count if exec_time_count > 0 else 0.0

    # Build per-symbol breakdown
    symbol_breakdown = {}
    for sym, data in by_symbol.items():
        symbol_breakdown[sym] = {
            "total_trades": data["count"],
            "avg_slippage": round(data["total_slippage"] / data["slippage_count"], 5) if data["slippage_count"] > 0 else 0.0,
            "avg_execution_time_ms": round(data["total_exec_time"] / data["exec_time_count"], 1) if data["exec_time_count"] > 0 else 0.0,
        }

    # Build per-type breakdown
    type_breakdown = {}
    for tt, data in by_type.items():
        type_breakdown[tt] = {
            "total_trades": data["count"],
            "avg_slippage": round(data["total_slippage"] / data["slippage_count"], 5) if data["slippage_count"] > 0 else 0.0,
            "avg_execution_time_ms": round(data["total_exec_time"] / data["exec_time_count"], 1) if data["exec_time_count"] > 0 else 0.0,
        }

    return {
        "total_filled": total_filled,
        "avg_slippage": round(avg_slippage, 5),
        "avg_execution_time_ms": round(avg_exec_time, 1),
        "slippage_coverage": round(slippage_count / total_filled * 100, 1) if total_filled > 0 else 0.0,
        "by_symbol": symbol_breakdown,
        "by_trading_type": type_breakdown,
    }
