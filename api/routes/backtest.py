"""
Backtest REST endpoints.

POST /backtest/run              — run a strategy simulation on historical MT5 data
GET  /backtest/strategies       — list available strategies per trading type
GET  /backtest/history          — paginated list of saved backtest runs
GET  /backtest/history/{run_id} — full result for a specific run
DELETE /backtest/history/{run_id} — delete a saved run
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from engine.backtester import run_backtest, BT_TIMEFRAME, BT_STRATEGY_TIMEFRAME, BT_EXTRA_TIMEFRAMES

router = APIRouter()

TRADING_TYPE = Literal["scalping", "day_trading", "swing"]

STRATEGIES_BY_TYPE: dict[str, list[str]] = {
    "scalping":    ["ema_scalp", "bb_squeeze", "vwap_reversion"],
    "day_trading": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "swing":       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
}

# Persistent storage — one JSON file per run, index file for fast listing
_HISTORY_DIR = Path("data/backtest_history")
_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
_INDEX_FILE  = _HISTORY_DIR / "_index.jsonl"
_MAX_BACKTEST_RUNS = 200   # L-5: cap stored runs to prevent unbounded disk growth


# ── Persistence helpers ───────────────────────────────────────────────────

def _append_index(entry: dict) -> None:
    with open(_INDEX_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _read_index() -> list[dict]:
    if not _INDEX_FILE.exists():
        return []
    entries = []
    with open(_INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    # Keep only entries whose run file still exists (handles deletions)
    return [e for e in entries if (_HISTORY_DIR / f"{e['id']}.json").exists()]


def _save_run(run_id: str, data: dict) -> None:
    path = _HISTORY_DIR / f"{run_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_run(run_id: str) -> dict | None:
    # Basic path traversal guard
    safe_id = Path(run_id).name
    path = _HISTORY_DIR / f"{safe_id}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _delete_run(run_id: str) -> bool:
    safe_id = Path(run_id).name
    path = _HISTORY_DIR / f"{safe_id}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def _prune_history() -> None:
    """Delete oldest runs when total exceeds _MAX_BACKTEST_RUNS."""
    index = _read_index()
    if len(index) <= _MAX_BACKTEST_RUNS:
        return
    index.sort(key=lambda e: e.get("run_at", ""), reverse=True)
    for old in index[_MAX_BACKTEST_RUNS:]:
        _delete_run(old["id"])


# ── Request model ─────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol:           str          = "EURUSD"
    strategy:         str          = "ema_scalp"
    trading_type:     TRADING_TYPE = "scalping"
    bars:             int          = Field(default=2000, ge=200, le=10000)
    initial_balance:  float        = Field(default=10_000.0, gt=0)
    risk_pct:         float        = Field(default=1.0, gt=0, le=10)
    use_ai_filters:   bool         = True   # apply LSTM+RL gate to match live conditions


# ── Routes ────────────────────────────────────────────────────────────────

@router.get("/strategies")
def list_strategies():
    """Return available strategy names grouped by trading type."""
    return STRATEGIES_BY_TYPE


@router.get("/history")
def get_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    trading_type: str | None = Query(default=None),
    symbol: str | None = Query(default=None),
    strategy: str | None = Query(default=None),
):
    """
    Return paginated list of saved backtest runs, newest first.
    Each item is a summary (no trade log) for fast loading.
    """
    index = _read_index()
    index.sort(key=lambda e: e.get("run_at", ""), reverse=True)

    # Optional filters
    if trading_type:
        index = [e for e in index if e.get("trading_type") == trading_type]
    if symbol:
        index = [e for e in index if e.get("symbol", "").upper() == symbol.upper()]
    if strategy:
        index = [e for e in index if e.get("strategy") == strategy]

    total = len(index)
    start = (page - 1) * page_size
    page_items = index[start : start + page_size]

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     (total + page_size - 1) // page_size if total else 0,
        "items":     page_items,
    }


@router.get("/history/{run_id}")
def get_run(run_id: str):
    """Return the full result (including trade log) for a saved backtest run."""
    data = _load_run(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id!r} not found")
    return data


@router.delete("/history/{run_id}")
def delete_run(run_id: str):
    """Delete a saved backtest run."""
    deleted = _delete_run(run_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Backtest run {run_id!r} not found")
    return {"deleted": run_id}


@router.get("/journal/replay")
async def replay_journal_trades(
    trading_type: str = Query("day_trading"),
    account: str = Query("all"),
    limit: int = Query(100),
):
    """
    Replay actual closed trades from the journal through current strategy params.
    Shows what the outcome would have been with current optimized params vs actual.
    Useful for detecting parameter overfitting on live trades.
    """
    from engine.trade_journal import trade_journal
    from ai.param_optimizer import optimizer

    entries = trade_journal.get(
        account=account,
        trading_type=trading_type,
        event="close",
        limit=limit,
    )
    closed = [e for e in entries if e.get("profit") is not None]

    comparison = []
    for e in closed:
        strat = e.get("comment", "").split("|")[0] if "|" in e.get("comment", "") else ""
        sym   = e.get("symbol", "")
        actual_profit = e.get("profit", 0)
        actual_outcome = "win" if actual_profit > 0 else "loss"

        # Get current optimized params for this strategy/symbol
        current_params = optimizer.get_params(strat, sym) if strat and sym else {}

        comparison.append({
            "ticket":         e.get("ticket"),
            "symbol":         sym,
            "strategy":       strat,
            "direction":      e.get("direction"),
            "entry":          e.get("entry"),
            "close_time":     e.get("close_time"),
            "actual_profit":  actual_profit,
            "actual_outcome": actual_outcome,
            "current_params": current_params,
            "confidence":     e.get("confidence"),
        })

    wins    = sum(1 for c in comparison if c["actual_outcome"] == "win")
    total   = len(comparison)
    return {
        "total":      total,
        "wins":       wins,
        "win_rate":   round(wins / total, 4) if total else 0,
        "trades":     comparison,
        "trading_type": trading_type,
        "account":    account,
    }

@router.post("/run")
async def backtest_run(req: BacktestRequest):
    """
    Run a walk-forward backtest for the given strategy and symbol.

    Fetches historical OHLCV from MT5, simulates every trade signal that fired,
    saves the full result to disk, and returns it with a run_id for future retrieval.
    """
    from api.main import get_mt5_client

    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 not connected")

    # Resolve primary TF — some strategies use a finer TF than the trading-type default
    primary_tf = BT_STRATEGY_TIMEFRAME.get(req.strategy) or BT_TIMEFRAME.get(req.trading_type, "H1")

    df = await asyncio.to_thread(client.get_ohlcv, req.symbol, primary_tf, req.bars)
    if df is None or df.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No OHLCV data for {req.symbol} ({primary_tf}). Is the symbol available in MT5?",
        )

    # Fetch secondary timeframes for multi-TF strategies (e.g. H1 trend for macd_ema_trend)
    # MATH-4 / GAP-7: both timeframes use the same `req.bars` count fetched at backtest-run
    # time, so all bars are contemporaneous ending at "now".  In a strict walk-forward
    # backtest the secondary TF slice would need to be aligned bar-by-bar with the primary
    # TF to avoid lookahead.  For the current exploratory/offline backtest use-case this
    # is acceptable: the higher-TF trend filter only reads broad directional context
    # (EMA/ADX slope) where a 1-bar misalignment has negligible practical effect.
    # A future walk-forward engine should align slices before calling the strategy.
    extra_dfs: dict = {}
    for kwarg, sec_tf in BT_EXTRA_TIMEFRAMES.get(req.strategy, {}).items():
        sec_df = await asyncio.to_thread(client.get_ohlcv, req.symbol, sec_tf, req.bars)
        if sec_df is not None and not sec_df.empty:
            extra_dfs[kwarg] = sec_df

    result = await asyncio.to_thread(
        run_backtest,
        req.strategy,
        req.symbol,
        df,
        req.trading_type,
        req.initial_balance,
        req.risk_pct,
        extra_dfs if extra_dfs else None,
        req.use_ai_filters,
    )

    if result is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy: {req.strategy!r}. "
                   f"Valid options: {STRATEGIES_BY_TYPE.get(req.trading_type, [])}",
        )

    # ── Persist to disk ───────────────────────────────────────────────────
    run_id  = str(uuid.uuid4())
    run_at  = datetime.now(timezone.utc).isoformat()
    payload = result.to_dict()
    payload["id"]     = run_id
    payload["run_at"] = run_at

    await asyncio.to_thread(_save_run, run_id, payload)

    # Index entry — summary only (no trade log / equity curve) for fast listing
    index_entry = {
        "id":             run_id,
        "run_at":         run_at,
        "symbol":         result.symbol,
        "strategy":       result.strategy,
        "trading_type":   result.trading_type,
        "timeframe":      result.timeframe,
        "bars_tested":    result.bars_tested,
        "total_trades":   result.total_trades,
        "win_rate":       result.win_rate,
        "profit_factor":  result.profit_factor,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio":   result.sharpe_ratio,
        "total_pnl_pct":  result.total_pnl_pct,
        "initial_balance": result.initial_balance,
        "risk_pct":       req.risk_pct,
    }
    await asyncio.to_thread(_append_index, index_entry)
    await asyncio.to_thread(_prune_history)

    # ── Auto-trigger param optimizer if enough backtest data accumulated ──
    # Only re-optimize if this run had meaningful trade volume (≥ 30 trades).
    # Re-uses the same df already fetched above so no second MT5 call is needed.
    if result.total_trades >= 30:
        try:
            from ai.param_optimizer import optimizer as _optimizer
            _optimizer.optimize_async(
                req.strategy,
                req.symbol,
                df,
                req.trading_type,
            )
        except Exception:
            pass  # optimizer trigger is best-effort

    return payload
