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
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ai.predictor import predictor, TRADING_TYPE_TF
from ai.rl_agent import rl_manager
from ai.trade_memory import memory

router = APIRouter()

TRADING_TYPE = Literal["scalping", "day_trading", "swing"]

# Default bar counts per trading type — match run_retrain.py.
_TRAIN_BARS: dict[str, int] = {
    "scalping":    200_000,  # M5  ≈ 2 yr
    "day_trading": 20_000,  # H1  ≈ 2 yr
    "swing":        15_000,  # H4  ≈ 2 yr
}


class TrainRequest(BaseModel):
    trading_type: TRADING_TYPE = "day_trading"
    bars: int = 0   # 0 = auto-select by trading_type (recommended)


@router.get("/status")
def ai_status():
    """Return LSTM training status for all symbols."""
    return predictor.status()


# ───────────────────────────────────
# Train-all (must be declared BEFORE /{symbol})
# ───────────────────────────────────

class TrainAllRequest(BaseModel):
    bars: int = 0   # 0 = auto-select by trading_type (recommended)


@router.post("/train/all")
async def train_all_symbols(req: TrainAllRequest = TrainAllRequest()):
    """
    Queues LSTM retraining for all enabled symbols × trading types and returns
    immediately.  A background task does fetching + dispatching in batches:

        for each mode  (scalping → day_trading → swing)
            for each pair  → fetch OHLCV + sleep 300 ms
            sleep 2 s      ← mode boundary rest
            for each pair  → dispatch train thread + sleep 100 ms
            sleep 2 s      ← mode boundary rest between trains
    """
    from api.main import get_mt5_client
    import json

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    CONFIG_PATH = Path(__file__).parent.parent.parent / "config"
    try:
        symbols_cfg = json.loads((CONFIG_PATH / "symbols.json").read_text(encoding="utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=500, detail="Cannot read symbols.json")

    async def _bg_task() -> None:
        for trading_type, sym_list in symbols_cfg.items():
            if not isinstance(sym_list, list):
                continue
            tf_str  = TRADING_TYPE_TF.get(trading_type, "H1")
            entries = [
                (e.get("symbol") if isinstance(e, dict) else e)
                for e in sym_list
                if e and (e.get("enabled", False) if isinstance(e, dict) else True)
            ]

            # ── Phase 1: fetch each symbol, 300 ms between pairs ──────────────
            mode_bars = req.bars if req.bars > 0 else _TRAIN_BARS.get(trading_type, 5_000)
            ohlcv: dict[str, object] = {}
            for symbol in entries:
                if not symbol or predictor.is_training(symbol, trading_type):
                    continue
                df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, mode_bars)
                ohlcv[symbol] = df
                await asyncio.sleep(0.3)  # rest between pairs

            # ── Mode boundary rest ─────────────────────────────────────────────
            await asyncio.sleep(2.0)

            # ── Phase 2: dispatch training threads, 100 ms between dispatches ──
            for symbol, df in ohlcv.items():
                if df is None or df.empty:
                    continue
                predictor.train_async(symbol, df, trading_type)
                await asyncio.sleep(0.1)  # rest between pairs

            # ── Mode boundary rest between train dispatches ────────────────────
            await asyncio.sleep(2.0)

    asyncio.create_task(_bg_task())
    return {"status": "queued", "detail": "Training running in background — poll /ai/status"}


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
    bars   = req.bars if req.bars > 0 else _TRAIN_BARS.get(req.trading_type, 5_000)
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, bars)
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
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, 100)
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
    from engine.account_store import current_mode as _cm
    _mode = _cm()
    # Delete the mode-suffixed file that is actually in use (e.g. rl_qtable_scalping_live.json)
    path = DATA_DIR / f"rl_qtable_{trading_type}_{_mode}.json"
    if path.exists():
        os.remove(path)
    # Reinitialise agent
    from ai.rl_agent import RLAgent
    rl_manager._agents[trading_type] = RLAgent(trading_type, mode=_mode)
    return {"status": "reset", "trading_type": trading_type, "mode": _mode}


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


# ───────────────────────────────────
# Parameter Optimizer endpoints
# ───────────────────────────────────

from ai.param_optimizer import optimizer as _optimizer, PARAM_GRIDS, MAX_CONCURRENT_OPT


@router.get("/optimizer/status")
def optimizer_status():
    """Return current optimizer status for all (strategy, symbol) pairs."""
    return {
        "jobs":        _optimizer.status(),
        "param_grids": {k: list(v.keys()) for k, v in PARAM_GRIDS.items()},
    }


# Bars to fetch per trading type — matches run_optimizer.py standalone values.
# These cover 2+ years of history so UI-triggered optimizations are consistent
# with the standalone run and will NOT downgrade already-optimized results.
_OPT_BARS: dict[str, int] = {
    "scalping":    99_000,   # M5  ~2 years
    "day_trading": 17_000,   # H1  ~2 years
    "swing":        5_000,   # H4  ~2 years
}


class OptimizeRequest(BaseModel):
    trading_type: TRADING_TYPE = "day_trading"
    bars: int = 0  # 0 = auto (uses _OPT_BARS[trading_type]); set explicitly to override


@router.post("/optimizer/run/{strategy_name}/{symbol}")
async def run_optimizer(
    strategy_name: str,
    symbol: str,
    req: OptimizeRequest = OptimizeRequest(),
):
    """
    Trigger backtest grid-search optimization for one (strategy, symbol) pair.
    Runs in background — poll GET /ai/optimizer/status to track progress.
    """
    from api.main import get_mt5_client

    if strategy_name not in PARAM_GRIDS:
        raise HTTPException(status_code=400, detail=f"Unknown strategy: {strategy_name}")

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    tf_str  = TRADING_TYPE_TF.get(req.trading_type, "H1")
    n_bars  = req.bars or _OPT_BARS.get(req.trading_type, 17_000)
    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, n_bars)
    if df is None or df.empty:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {symbol}/{tf_str}")

    started = _optimizer.optimize_async(strategy_name, symbol, df, req.trading_type)
    return {
        "status":         "optimization_started" if started else "already_running",
        "strategy":       strategy_name,
        "symbol":         symbol,
        "trading_type":   req.trading_type,
        "bars":           len(df),
    }


@router.post("/optimizer/run/all")
async def run_optimizer_all(req: OptimizeRequest = OptimizeRequest()):
    """
    Queues optimization for every (strategy, enabled-symbol) pair and returns
    immediately.  A background task does all MT5 fetching + dispatching in
    batched steps with deliberate rest intervals so the live bot is never
    starved of the MT5 lock:

        for each mode  (scalping → day_trading → swing)
            for each pair  → fetch OHLCV + sleep 300 ms
            sleep 2 s      ← mode boundary rest
            for each strategy
                for each pair  → dispatch optimizer thread + sleep 100 ms
                sleep 1 s      ← strategy boundary rest
    """
    from api.main import get_mt5_client
    import json

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    CONFIG_PATH = Path(__file__).parent.parent.parent / "config"
    try:
        symbols_cfg    = json.loads((CONFIG_PATH / "symbols.json").read_text(encoding="utf-8-sig"))
        strategies_cfg = json.loads((CONFIG_PATH / "strategies.json").read_text(encoding="utf-8-sig"))
    except Exception:
        raise HTTPException(status_code=500, detail="Cannot read config files")

    async def _bg_task() -> None:
        ohlcv_cache: dict[tuple[str, str], object] = {}

        for trading_type in ("scalping", "day_trading", "swing"):
            active   = strategies_cfg.get(trading_type, {}).get("active_strategies", [])
            tf_str   = TRADING_TYPE_TF.get(trading_type, "H1")
            sym_list = symbols_cfg.get(trading_type, [])
            symbols  = [
                (e.get("symbol") if isinstance(e, dict) else e)
                for e in sym_list
                if (e.get("enabled", False) if isinstance(e, dict) else True)
            ]

            # ── Phase 1: fetch each (symbol, tf) once, 300 ms between pairs ──
            n_bars = req.bars or _OPT_BARS.get(trading_type, 17_000)
            for symbol in symbols:
                if not symbol:
                    continue
                cache_key = (symbol, tf_str)
                if cache_key not in ohlcv_cache:
                    df = await asyncio.to_thread(client.get_ohlcv, symbol, tf_str, n_bars)
                    ohlcv_cache[cache_key] = df
                await asyncio.sleep(0.3)  # rest between pairs — gives live bot MT5 lock

            # ── Mode boundary rest ─────────────────────────────────────────────
            await asyncio.sleep(2.0)

            # ── Phase 2: dispatch optimizer threads, 100 ms between dispatches ──
            for strat in active:
                if strat not in PARAM_GRIDS:
                    continue
                for symbol in symbols:
                    if not symbol:
                        continue
                    df = ohlcv_cache.get((symbol, tf_str))
                    if df is None or df.empty:
                        continue
                    # Wait for a free concurrency slot before dispatching
                    while sum(1 for j in _optimizer.status().values() if j.get("running")) >= MAX_CONCURRENT_OPT:
                        await asyncio.sleep(5.0)
                    _optimizer.optimize_async(strat, symbol, df, trading_type)
                    await asyncio.sleep(0.1)  # rest between pairs

                # ── Strategy boundary rest ─────────────────────────────────────
                await asyncio.sleep(1.0)

    asyncio.create_task(_bg_task())
    return {"status": "queued", "detail": "Optimizer running in background — poll /ai/optimizer/status"}

