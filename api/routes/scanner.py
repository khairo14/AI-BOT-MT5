"""
Scanner routes — market scanner for discovering tradeable symbols.
Scans 150+ symbols from MT5 broker, ranks by viability for scalping/day/swing.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.util import get_remote_address

from api.dependencies import get_client
from engine.mt5_client import MT5Client
from engine.market_scanner import MarketScanner, ScanSummary, summary_to_dict

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

# Global scanner instance (initialized on first use)
_scanner: Optional[MarketScanner] = None


def get_scanner(client: MT5Client = Depends(get_client)) -> MarketScanner:
    """Return the shared scanner instance, creating it on first access."""
    global _scanner
    if _scanner is None:
        _scanner = MarketScanner(client)
        logger.info("MarketScanner instance created")
    return _scanner


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    """Request to trigger a new scan."""
    trading_type: Optional[str] = None  # "scalping", "day_trading", "swing", or None for all
    force_refresh: bool = False


class AddSymbolRequest(BaseModel):
    """Request to add a symbol to enabled symbols for a trading type."""
    symbol: str
    trading_type: str  # "scalping", "day_trading", or "swing"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
@limiter.limit("60/minute")  # Task #7: Rate limiting
async def get_scan_results(
    request: Request,
    scanner: MarketScanner = Depends(get_scanner),
    force_refresh: bool = Query(False, description="Force rescan instead of using cache")
):
    """
    Get market scan results for all trading types.
    Returns cached results if available and fresh, otherwise triggers new scan.
    
    Query params:
    - force_refresh: If true, bypass cache and rescan all symbols
    """
    try:
        # Run scan in thread pool to avoid blocking
        summary = await asyncio.to_thread(scanner.scan_all, force_refresh=force_refresh)
        
        return {
            "status": "success",
            "data": summary_to_dict(summary)
        }
    
    except Exception as e:
        logger.error(f"Scanner error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scanner failed: {str(e)}")


@router.get("/type/{trading_type}")
@limiter.limit("60/minute")  # Task #7: Rate limiting
async def get_scan_by_type(
    request: Request,
    trading_type: str,
    scanner: MarketScanner = Depends(get_scanner),
    force_refresh: bool = Query(False, description="Force rescan instead of using cache")
):
    """
    Get market scan results for a specific trading type.
    
    Path params:
    - trading_type: "scalping", "day_trading", or "swing"
    
    Query params:
    - force_refresh: If true, bypass cache and rescan
    """
    if trading_type not in ["scalping", "day_trading", "swing"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid trading_type: {trading_type}. Must be scalping, day_trading, or swing."
        )
    
    try:
        results = await asyncio.to_thread(scanner.scan_type, trading_type, force_refresh=force_refresh)
        
        return {
            "status": "success",
            "trading_type": trading_type,
            "count": len(results),
            "results": [
                {
                    "symbol": r.symbol,
                    "category": r.category,
                    "composite_score": r.composite_score,
                    "atr_pips": r.atr_pips,
                    "spread_pips": r.spread_pips,
                    "adx": r.adx,
                    "volatility_score": r.volatility_score,
                    "spread_score": r.spread_score,
                    "trend_score": r.trend_score,
                    "liquidity_subscore": r.liquidity_subscore,
                    "momentum_subscore": r.momentum_subscore,
                    "current_price": r.current_price,
                    "trading_hours_active": r.trading_hours_active,
                    "last_updated": r.last_updated
                }
                for r in results
            ]
        }
    
    except Exception as e:
        logger.error(f"Scanner error for {trading_type}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scanner failed: {str(e)}")


@router.post("/scan")
@limiter.limit("30/minute")  # Task #7: Rate limiting (compute-intensive)
async def trigger_scan(
    request: Request,
    body: ScanRequest,
    scanner: MarketScanner = Depends(get_scanner)
):
    """
    Manually trigger a market scan.
    
    Body:
    - trading_type: Optional, scan only specific type (scalping/day_trading/swing)
    - force_refresh: If true, bypass cache
    """
    try:
        if body.trading_type:
            if body.trading_type not in ["scalping", "day_trading", "swing"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid trading_type: {body.trading_type}"
                )
            
            results = await asyncio.to_thread(
                scanner.scan_type, 
                body.trading_type, 
                force_refresh=body.force_refresh
            )
            
            return {
                "status": "success",
                "message": f"Scan complete for {body.trading_type}",
                "trading_type": body.trading_type,
                "count": len(results),
                "results": [
                    {
                        "symbol": r.symbol,
                        "composite_score": r.composite_score,
                        "atr_pips": r.atr_pips,
                        "spread_pips": r.spread_pips
                    }
                    for r in results
                ]
            }
        
        else:
            summary = await asyncio.to_thread(
                scanner.scan_all,
                force_refresh=body.force_refresh
            )
            
            totals = {
                ttype: len(results) 
                for ttype, results in summary.trading_types.items()
            }
            
            return {
                "status": "success",
                "message": "Full market scan complete",
                "timestamp": summary.timestamp,
                "total_scanned": summary.total_scanned,
                "total_passed": summary.total_passed,
                "scan_duration": f"{summary.scan_duration_seconds:.2f}s",
                "results_per_type": totals
            }
    
    except Exception as e:
        logger.error(f"Manual scan trigger failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")


@router.get("/cache")
@limiter.limit("100/minute")  # Task #7: Rate limiting
async def get_cache_status(request: Request, scanner: MarketScanner = Depends(get_scanner)):
    """
    Get scanner cache status.
    Returns cached data timestamp and validity.
    """
    cached = scanner.get_cached_results()
    
    if cached is None:
        return {
            "status": "no_cache",
            "message": "No cached results available"
        }
    
    cache_age_minutes = (
        datetime.now(timezone.utc) - datetime.fromisoformat(cached.timestamp)
    ).total_seconds() / 60
    
    cache_duration = scanner.cfg.get("advanced", {}).get("cache_duration_minutes", 30)
    is_valid = cache_age_minutes < cache_duration
    
    return {
        "status": "cached",
        "is_valid": is_valid,
        "timestamp": cached.timestamp,
        "age_minutes": round(cache_age_minutes, 1),
        "cache_duration_minutes": cache_duration,
        "total_scanned": cached.total_scanned,
        "total_passed": cached.total_passed,
        "scan_duration_seconds": cached.scan_duration_seconds
    }


@router.post("/cache/invalidate")
@limiter.limit("30/minute")  # Task #7: Rate limiting (cache operation)
async def invalidate_cache(request: Request, scanner: MarketScanner = Depends(get_scanner)):
    """
    Manually invalidate the scanner cache.
    Next scan request will trigger a fresh scan.
    """
    scanner.invalidate_cache()
    return {
        "status": "success",
        "message": "Scanner cache invalidated"
    }


@router.post("/add-symbol")
@limiter.limit("60/minute")  # Task #7: Rate limiting (write operation)
async def add_symbol_to_config(request: Request, body: AddSymbolRequest):
    """
    Add a scanned symbol to the strategy scanner (active trading symbols).
    Updates config/scanner.json (used by strategy scanner on trading pages).
    
    Body:
    - symbol: Symbol name (e.g., "EURUSD")
    - trading_type: "scalping", "day_trading", or "swing"
    - enabled: true/false (default: true)
    """
    import json
    import os
    
    if body.trading_type not in ["scalping", "day_trading", "swing"]:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid trading_type: {body.trading_type}"
        )
    
    try:
        # Load current strategy scanner config
        scanner_path = "config/scanner.json"
        if not os.path.exists(scanner_path):
            raise HTTPException(status_code=500, detail="scanner.json not found")
        
        with open(scanner_path, "r") as f:
            scanner_cfg = json.load(f)
        
        # Get symbols list for this trading type
        if body.trading_type not in scanner_cfg:
            scanner_cfg[body.trading_type] = {
                "enabled": True,
                "symbols": [],
                "timeframe": "M5" if body.trading_type == "scalping" else "H1"
            }
        
        symbols_list = scanner_cfg[body.trading_type].get("symbols", [])
        
        # Check if symbol already exists
        if body.symbol in symbols_list:
            message = f"{body.symbol} already in {body.trading_type} strategy scanner"
        else:
            # Add symbol to list
            symbols_list.append(body.symbol)
            scanner_cfg[body.trading_type]["symbols"] = symbols_list
            
            # Save updated config
            with open(scanner_path, "w") as f:
                json.dump(scanner_cfg, f, indent=2)
            
            message = f"Added {body.symbol} to {body.trading_type} strategy scanner"
        
        logger.info(message)
        
        return {
            "status": "success",
            "message": message,
            "symbol": body.symbol,
            "trading_type": body.trading_type,
            "total_symbols": len(symbols_list)
        }
    
    except Exception as e:
        logger.error(f"Failed to add symbol: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to add symbol: {str(e)}")


@router.get("/performance")
@limiter.limit("60/minute")  # Task #7: Rate limiting
async def get_scanner_performance(request: Request):
    """
    Get live scanner performance metrics:
    - Active pairs by trading type (from scanner.json)
    - Signal & trade stats per pair from trade journal
    - Pair rotation history
    Returns active vs inactive pair breakdown.
    """
    import json
    import os
    from collections import defaultdict
    from pathlib import Path
    
    try:
        # Load scanner config (active pairs)
        scanner_path = "config/scanner.json"
        if not os.path.exists(scanner_path):
            raise HTTPException(status_code=500, detail="scanner.json not found")
        
        with open(scanner_path, "r") as f:
            scanner_cfg = json.load(f)
        
        # Load trade journal for stats
        journal_path = "data/trade_journal.jsonl"
        pair_stats = defaultdict(lambda: {
            "signals": 0, "trades": 0, "wins": 0, 
            "losses": 0, "total_profit": 0.0, "last_signal": None
        })
        
        if os.path.exists(journal_path):
            with open(journal_path, "r") as f:
                for line in f:
                    try:
                        trade = json.loads(line)
                        symbol = trade.get("symbol")
                        if not symbol:
                            continue
                        
                        trade_type = trade.get("trading_type", "")
                        key = (symbol, trade_type)
                        event = trade.get("event")
                        profit = trade.get("profit", 0.0) or 0.0
                        
                        if event == "close":
                            pair_stats[key]["trades"] += 1
                            if profit > 0:
                                pair_stats[key]["wins"] += 1
                            elif profit < 0:
                                pair_stats[key]["losses"] += 1
                            pair_stats[key]["total_profit"] += profit
                            pair_stats[key]["last_signal"] = trade.get("close_time")
                    except:
                        continue
        
        # Build active/inactive breakdown by trading type
        result = {}
        
        for trading_type in ["scalping", "day_trading", "swing"]:
            type_cfg = scanner_cfg.get(trading_type, {})
            enabled = type_cfg.get("enabled", False)
            symbols = type_cfg.get("symbols", [])
            
            active_pairs = []
            for sym in symbols:
                stats = pair_stats[(sym, trading_type)]
                win_rate = (
                    (stats["wins"] / stats["trades"] * 100) 
                    if stats["trades"] > 0 else 0.0
                )
                
                active_pairs.append({
                    "symbol": sym,
                    "signals": stats["signals"],
                    "trades": stats["trades"],
                    "wins": stats["wins"],
                    "losses": stats["losses"],
                    "win_rate": round(win_rate, 1),
                    "total_profit": round(stats["total_profit"], 2),
                    "last_signal": stats["last_signal"],
                    "status": "active" if enabled else "paused"
                })
            
            result[trading_type] = {
                "enabled": enabled,
                "total_active": len(symbols),
                "active_pairs": active_pairs,
                "total_signals": sum(p["signals"] for p in active_pairs),
                "total_trades": sum(p["trades"] for p in active_pairs),
                "total_profit": round(sum(p["total_profit"] for p in active_pairs), 2),
                "avg_win_rate": round(
                    sum(p["win_rate"] for p in active_pairs) / len(active_pairs)
                    if active_pairs else 0.0,
                    1
                )
            }
        
        return {
            "status": "success",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trading_types": result
        }
    
    except Exception as e:
        logger.error(f"Failed to get scanner performance: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to get performance: {str(e)}")


@router.get("/config")
@limiter.limit("100/minute")  # Task #7: Rate limiting
async def get_scanner_system_config(request: Request):
    """
    Get current market scanner configuration (criteria, weights, filters).
    This is separate from the strategy scanner (active symbols).
    Returns criteria from config/market_scanner.json.
    """
    import json
    import os
    
    config_path = "config/market_scanner.json"
    if not os.path.exists(config_path):
        raise HTTPException(status_code=500, detail="market_scanner.json not found")
    
    with open(config_path, "r") as f:
        cfg = json.load(f)
    
    return {
        "status": "success",
        "config": {
            "enabled": cfg.get("enabled", True),
            "scan_interval_minutes": cfg.get("scan_interval_minutes"),
            "max_results_per_type": cfg.get("max_results_per_type"),
            "trading_types": cfg.get("trading_types"),
            "categories": cfg.get("categories"),
            "filters": cfg.get("filters"),
            "advanced": cfg.get("advanced")
        }
    }


@router.get("/health")
@limiter.limit("60/minute")  # Task #7: Rate limiting
async def scanner_health(request: Request, scanner: MarketScanner = Depends(get_scanner)):
    """
    Health check for scanner system.
    Returns operational status and last scan info.
    """
    cached = scanner.get_cached_results()
    
    return {
        "status": "healthy",
        "scanner_enabled": scanner.cfg.get("enabled", True),
        "mt5_connected": scanner.mt5.is_connected() if scanner.mt5 else False,
        "cache_valid": scanner._is_cache_valid(),
        "last_scan": cached.timestamp if cached else None,
        "total_scanned_last": cached.total_scanned if cached else 0,
        "total_passed_last": cached.total_passed if cached else 0
    }
