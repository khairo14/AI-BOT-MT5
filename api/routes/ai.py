"""
AI REST endpoints.

LSTM:
  GET  /ai/status                       — training status for all symbol+type keys
  POST /ai/train/{symbol}               — trigger LSTM training (background)
  GET  /ai/confidence/{symbol}          — LSTM directional probability

RL Agent:
  GET  /ai/rl/status                    — RL agent params per trading type
  POST /ai/rl/reset/{trading_type}      — reset RL agent to defaults

Trade Memory:
  GET  /ai/memory/stats                 — win rate, avg PnL, etc.
  GET  /ai/memory/recent                — last N trade outcomes
"""

from __future__ import annotations

import asyncio
from typing import Literal, Optional

import MetaTrader5 as mt5
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.predictor import predictor, TRADING_TYPE_TF
from ai.rl_agent import rl_manager
from ai.trade_memory import memory

router = APIRouter()

TRADING_TYPE = Literal["scalping", "day_trading", "swing"]

MT5_TF_MAP = {
    "M5": mt5.TIMEFRAME_M5,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


class TrainRequest(BaseModel):
    trading_type: TRADING_TYPE = "day_trading"
    bars: int = 1000   # bars of the trading_type timeframe


@router.get("/status")
def ai_status():
    """Return LSTM training status for all symbols."""
    return predictor.status()


@router.post("/train/{symbol}")
async def train_symbol(symbol: str, req: TrainRequest = TrainRequest()):
    """
    Trigger background LSTM training for a symbol.
    Uses the natural timeframe for the trading_type:
      scalping → M5, day_trading → H1, swing → H4
    Returns immediately — poll GET /ai/status to check completion.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    if predictor.is_training(symbol, req.trading_type):
        return {"status": "already_training", "symbol": symbol, "trading_type": req.trading_type}

    tf_str = TRADING_TYPE_TF.get(req.trading_type, "H1")
    tf_mt5 = MT5_TF_MAP[tf_str]
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_mt5, req.bars)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {symbol} ({tf_str})")

    started = predictor.train_async(symbol, df, req.trading_type)
    return {
        "status":       "training_started" if started else "already_training",
        "symbol":       symbol,
        "trading_type": req.trading_type,
        "timeframe":    tf_str,
        "bars":         len(df),
    }


@router.get("/confidence/{symbol}")
async def get_confidence(symbol: str, trading_type: TRADING_TYPE = "day_trading"):
    """
    Get LSTM directional probability for a symbol+trading_type.
    p_up + p_down = 1.0. Returns 0.5/0.5 if no model is trained yet.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    tf_str = TRADING_TYPE_TF.get(trading_type, "H1")
    tf_mt5 = MT5_TF_MAP[tf_str]
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_mt5, 100)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No data for {symbol} ({tf_str})")

    p_up = predictor.predict(symbol, df, trading_type)
    key  = f"{symbol}_{trading_type}"
    return {
        "symbol":        symbol,
        "trading_type":  trading_type,
        "timeframe":     tf_str,
        "p_up":          round(p_up, 4),
        "p_down":        round(1.0 - p_up, 4),
        "model_trained": predictor.is_trained(symbol, trading_type),
        **predictor.status().get(key, {}),
    }


# ───────────────────────────────────
# RL Agent endpoints
# ───────────────────────────────────

@router.get("/rl/status")
def rl_status():
    """Return current RL agent parameters for all trading types."""
    return rl_manager.status()


@router.post("/rl/reset/{trading_type}")
def rl_reset(trading_type: TRADING_TYPE):
    """Reset a specific RL agent back to default thresholds (useful for testing)."""
    import os
    from ai.rl_agent import DATA_DIR, DEFAULT_CONF_THRESH, DEFAULT_RISK_FACTOR
    path = DATA_DIR / f"rl_qtable_{trading_type}.json"
    if path.exists():
        os.remove(path)
    # Reinitialise agent
    from ai.rl_agent import RLAgent
    rl_manager._agents[trading_type] = RLAgent(trading_type)
    return {"status": "reset", "trading_type": trading_type}


# ───────────────────────────────────
# Trade Memory endpoints
# ───────────────────────────────────

@router.get("/memory/stats")
def memory_stats(trading_type: Optional[TRADING_TYPE] = None):
    """Return aggregate win rate, avg P&L, SL/TP hit counts."""
    return memory.stats(trading_type=trading_type)


@router.get("/memory/recent")
def memory_recent(n: int = 50, trading_type: Optional[TRADING_TYPE] = None):
    """Return the last N trade outcomes, newest last."""
    return memory.recent(n=min(n, 500), trading_type=trading_type)
