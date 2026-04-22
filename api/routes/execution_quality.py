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


def _pip_size(symbol: str) -> Optional[float]:
    """Return pip size for a symbol (used to convert legacy raw-price slippage to pips).
    Returns None for unrecognised instrument types (e.g. individual stocks) so
    the caller can skip conversion rather than applying the wrong pip size."""
    s = symbol.upper()
    if any(x in s for x in ("JPY",)):
        return 0.01
    if any(x in s for x in ("GOLD", "XAUUSD", "SILVER", "XAGUSD", "OIL", "BRENT", "NGAS")):
        return 0.01
    if any(x in s for x in ("US30", "US100", "US500", "GER40", "UK100")):
        return 1.0
    if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "BCH", "LTC", "XLM", "ADA", "BNB")):
        return 1.0
    # 6-char forex pairs (e.g. EURUSD, GBPJPY already caught above)
    if len(s) == 6 and s.isalpha():
        return 0.0001
    # Unknown — likely a stock; return None to avoid misclassification
    return None


def _to_pips(slippage: float, symbol: str) -> float:
    """
    Normalise a slippage value to pips.

    Records written before the pip-conversion fix store a raw price diff
    (e.g. 0.00021 for GBPUSD).  Records written after store pips directly
    (e.g. 2.1 for forex) or raw $ distance (e.g. 0.02 for stocks).

    For unknown instruments (stocks) _pip_size returns None — in that case
    return the stored value as-is; order_manager already stores stocks as
    raw $ distance so no conversion is needed.
    """
    pip = _pip_size(symbol)
    if pip is None:
        # Unknown instrument type (e.g. stock) — value is already in the
        # correct display unit (raw $ distance), return as-is.
        return round(slippage, 2)
    # If stored value already looks pip-scale (>= 0.1) treat as pips
    if slippage >= 0.1:
        return round(slippage, 2)
    # Otherwise it's a legacy raw price diff — convert
    return round(slippage / pip, 2) if pip > 0 else slippage


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
    total_spread = 0.0
    slippage_count = 0
    exec_time_count = 0
    spread_count = 0

    by_symbol = defaultdict(lambda: {
        "count": 0,
        "total_slippage": 0.0,
        "slippage_count": 0,
        "total_exec_time": 0.0,
        "exec_time_count": 0,
        "total_spread": 0.0,
        "spread_count": 0,
    })

    by_type = defaultdict(lambda: {
        "count": 0,
        "total_slippage": 0.0,
        "slippage_count": 0,
        "total_exec_time": 0.0,
        "exec_time_count": 0,
        "total_spread": 0.0,
        "spread_count": 0,
    })

    for entry in entries:
        total_filled += 1
        symbol = entry.get("symbol", "UNKNOWN")
        tt = entry.get("trading_type", "unknown")

        # Slippage tracking — normalise to pips (handles legacy raw-price records)
        slippage_raw = entry.get("slippage")
        if slippage_raw is not None and slippage_raw > 0:
            slippage = _to_pips(slippage_raw, symbol)
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

        # Spread tracking
        spread = entry.get("spread_pips")
        if spread is not None and spread > 0:
            total_spread += spread
            spread_count += 1
            by_symbol[symbol]["total_spread"] += spread
            by_symbol[symbol]["spread_count"] += 1
            by_type[tt]["total_spread"] += spread
            by_type[tt]["spread_count"] += 1

        by_symbol[symbol]["count"] += 1
        by_type[tt]["count"] += 1

    # Calculate averages
    avg_slippage = total_slippage / slippage_count if slippage_count > 0 else 0.0
    avg_exec_time = total_exec_time / exec_time_count if exec_time_count > 0 else 0.0
    avg_spread = total_spread / spread_count if spread_count > 0 else 0.0

    # Build per-symbol breakdown
    symbol_breakdown = {}
    for sym, data in by_symbol.items():
        symbol_breakdown[sym] = {
            "total_filled": data["count"],
            "avg_slippage": round(data["total_slippage"] / data["slippage_count"], 5) if data["slippage_count"] > 0 else None,
            "avg_execution_time_ms": round(data["total_exec_time"] / data["exec_time_count"], 1) if data["exec_time_count"] > 0 else None,
            "avg_spread_pips": round(data["total_spread"] / data["spread_count"], 2) if data["spread_count"] > 0 else None,
        }

    # Build per-type breakdown
    type_breakdown = {}
    for tt, data in by_type.items():
        type_breakdown[tt] = {
            "total_filled": data["count"],
            "avg_slippage": round(data["total_slippage"] / data["slippage_count"], 5) if data["slippage_count"] > 0 else None,
            "avg_execution_time_ms": round(data["total_exec_time"] / data["exec_time_count"], 1) if data["exec_time_count"] > 0 else None,
            "avg_spread_pips": round(data["total_spread"] / data["spread_count"], 2) if data["spread_count"] > 0 else None,
        }

    return {
        "total_filled": total_filled,
        "avg_slippage": round(avg_slippage, 5),
        "avg_execution_time_ms": round(avg_exec_time, 1),
        "avg_spread_pips": round(avg_spread, 2),
        "slippage_coverage": round(slippage_count / total_filled * 100, 1) if total_filled > 0 else 0.0,
        "by_symbol": symbol_breakdown,
        "by_trading_type": type_breakdown,
    }
