"""
Scanner routes — market scanner for discovering tradeable symbols.
Scans 150+ symbols from MT5 broker, ranks by viability for scalping/day/swing.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from loguru import logger
from pydantic import BaseModel

from api.dependencies import get_client
from engine.mt5_client import MT5Client
from engine.market_scanner import MarketScanner, ScanSummary, summary_to_dict

router = APIRouter()

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
    enabled: bool = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
async def get_scan_results(
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
async def get_scan_by_type(
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
async def trigger_scan(
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
async def get_cache_status(scanner: MarketScanner = Depends(get_scanner)):
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
async def invalidate_cache(scanner: MarketScanner = Depends(get_scanner)):
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
async def add_symbol_to_config(body: AddSymbolRequest):
    """
    Add a scanned symbol to the enabled symbols list for a trading type.
    Updates config/symbols.json.
    
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
        # Load current symbols config
        symbols_path = "config/symbols.json"
        if not os.path.exists(symbols_path):
            raise HTTPException(status_code=500, detail="symbols.json not found")
        
        with open(symbols_path, "r") as f:
            symbols_cfg = json.load(f)
        
        # Check if symbol already exists
        existing = [
            s for s in symbols_cfg.get(body.trading_type, [])
            if s["symbol"] == body.symbol
        ]
        
        if existing:
            # Update existing entry
            for s in symbols_cfg[body.trading_type]:
                if s["symbol"] == body.symbol:
                    s["enabled"] = body.enabled
            message = f"Updated {body.symbol} enabled status to {body.enabled}"
        else:
            # Add new entry (need to determine category)
            # Import scanner to use category detection
            from engine.market_scanner import MarketScanner
            temp_scanner = MarketScanner(None)  # type: ignore
            category = temp_scanner._get_symbol_category(body.symbol) or "unknown"
            
            new_entry = {
                "symbol": body.symbol,
                "enabled": body.enabled,
                "category": category
            }
            
            if body.trading_type not in symbols_cfg:
                symbols_cfg[body.trading_type] = []
            
            symbols_cfg[body.trading_type].append(new_entry)
            message = f"Added {body.symbol} to {body.trading_type} symbols"
        
        # Save updated config
        with open(symbols_path, "w") as f:
            json.dump(symbols_cfg, f, indent=2)
        
        logger.info(message)
        
        return {
            "status": "success",
            "message": message,
            "symbol": body.symbol,
            "trading_type": body.trading_type,
            "enabled": body.enabled
        }
    
    except Exception as e:
        logger.error(f"Failed to add symbol: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to add symbol: {str(e)}")


@router.get("/config")
async def get_scanner_config(scanner: MarketScanner = Depends(get_scanner)):
    """
    Get current scanner configuration.
    Returns criteria, weights, and filters for all trading types.
    """
    return {
        "status": "success",
        "config": {
            "enabled": scanner.cfg.get("enabled", True),
            "scan_interval_minutes": scanner.cfg.get("scan_interval_minutes"),
            "max_results_per_type": scanner.cfg.get("max_results_per_type"),
            "trading_types": scanner.cfg.get("trading_types"),
            "categories": scanner.cfg.get("categories"),
            "filters": scanner.cfg.get("filters"),
            "advanced": scanner.cfg.get("advanced")
        }
    }


@router.get("/health")
async def scanner_health(scanner: MarketScanner = Depends(get_scanner)):
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
