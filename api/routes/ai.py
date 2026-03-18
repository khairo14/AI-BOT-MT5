"""
AI REST endpoints.

GET  /ai/status               — training status for all symbols
POST /ai/train/{symbol}       — trigger background LSTM training
GET  /ai/confidence/{symbol}  — current directional probability from LSTM
"""

from __future__ import annotations

import asyncio
from typing import Optional

import MetaTrader5 as mt5
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.predictor import predictor

router = APIRouter()


class TrainRequest(BaseModel):
    bars: int = 1000   # H1 bars to train on (1000 ≈ 6 weeks)


@router.get("/status")
def ai_status():
    """Return LSTM training status for all symbols."""
    return predictor.status()


@router.post("/train/{symbol}")
async def train_symbol(symbol: str, req: TrainRequest = TrainRequest()):
    """
    Trigger background LSTM training for a symbol using H1 OHLCV data.
    Returns immediately — poll GET /ai/status to check completion.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    if predictor.is_training(symbol):
        return {"status": "already_training", "symbol": symbol}

    df = await asyncio.to_thread(client.get_ohlcv, symbol, mt5.TIMEFRAME_H1, req.bars)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for symbol: {symbol}")

    started = predictor.train_async(symbol, df)
    return {
        "status":  "training_started" if started else "already_training",
        "symbol":  symbol,
        "bars":    len(df),
    }


@router.get("/confidence/{symbol}")
async def get_confidence(symbol: str):
    """
    Get the LSTM directional probability for a symbol using the latest 100 H1 bars.
    p_up + p_down = 1.0. Returns 0.5/0.5 if no model is trained yet.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    df = await asyncio.to_thread(client.get_ohlcv, symbol, mt5.TIMEFRAME_H1, 100)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No data for symbol: {symbol}")

    p_up = predictor.predict(symbol, df)
    return {
        "symbol":        symbol,
        "p_up":          round(p_up, 4),
        "p_down":        round(1.0 - p_up, 4),
        "model_trained": predictor.is_trained(symbol),
        **predictor.status().get(symbol, {}),
    }
