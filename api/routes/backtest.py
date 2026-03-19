"""
Backtest REST endpoints.

POST /backtest/run       — run a strategy simulation on historical MT5 data
GET  /backtest/strategies — list available strategies per trading type
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine.backtester import run_backtest, BT_TIMEFRAME

router = APIRouter()

TRADING_TYPE = Literal["scalping", "day_trading", "swing"]

STRATEGIES_BY_TYPE: dict[str, list[str]] = {
    "scalping":    ["ema_scalp", "bb_squeeze", "vwap_reversion"],
    "day_trading": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "swing":       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
}


class BacktestRequest(BaseModel):
    symbol:          str          = "EURUSD"
    strategy:        str          = "ema_scalp"
    trading_type:    TRADING_TYPE = "scalping"
    bars:            int          = Field(default=2000, ge=200, le=10000)
    initial_balance: float        = Field(default=10_000.0, gt=0)
    risk_pct:        float        = Field(default=1.0, gt=0, le=10)


@router.get("/strategies")
def list_strategies():
    """Return available strategy names grouped by trading type."""
    return STRATEGIES_BY_TYPE


@router.post("/run")
async def backtest_run(req: BacktestRequest):
    """
    Run a walk-forward backtest for the given strategy and symbol.

    Fetches historical OHLCV from MT5, simulates every trade signal that fired,
    and returns a full result including per-trade log, equity curve, and metrics.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    tf_str = BT_TIMEFRAME.get(req.trading_type, "H1")

    df = await asyncio.to_thread(client.get_ohlcv, req.symbol, tf_str, req.bars)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No OHLCV data for {req.symbol} ({tf_str}). Is the symbol available in MT5?",
        )

    result = await asyncio.to_thread(
        run_backtest,
        req.strategy,
        req.symbol,
        df,
        req.trading_type,
        req.initial_balance,
        req.risk_pct,
    )

    if result is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy: {req.strategy!r}. "
                   f"Valid options: {STRATEGIES_BY_TYPE.get(req.trading_type, [])}",
        )

    return result.to_dict()
