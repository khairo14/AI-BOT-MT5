"""
Trades routes — open positions, trade history, manual close, OHLCV data.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.dependencies import get_client
from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager, OrderRequest
from engine.risk_manager import RiskManager

router = APIRouter()


def _get_risk_manager() -> RiskManager:
    """Return the shared RiskManager created at startup, falling back to a fresh one."""
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is not None:
            return rm
    except Exception:
        pass
    return RiskManager()


class PlaceOrderRequest(BaseModel):
    symbol: str
    direction: str       # "BUY" or "SELL"
    sl: float
    tp: Optional[float] = None
    risk_pct: Optional[float] = None
    trading_mode: str    # "scalping" | "day_trading" | "swing"
    comment: str = ""


class ModifyPositionRequest(BaseModel):
    sl: Optional[float] = None
    tp: Optional[float] = None


@router.get("/positions")
def get_positions(
    symbol: Optional[str] = Query(None),
    client: MT5Client = Depends(get_client),
):
    """Return all currently open positions, optionally filtered by symbol."""
    return client.get_open_positions(symbol=symbol)


@router.get("/history")
def get_history(
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    client: MT5Client = Depends(get_client),
):
    """Return closed trade history for the connected account."""
    return client.get_trade_history(date_from=date_from, date_to=date_to)


@router.get("/ohlcv/{symbol}/{timeframe}")
def get_ohlcv(
    symbol: str,
    timeframe: str,
    count: int = Query(500, ge=1, le=5000),
    client: MT5Client = Depends(get_client),
):
    """
    Return OHLCV candles for a symbol and timeframe.
    Used by the dashboard chart to populate historical data.
    """
    df = client.get_ohlcv(symbol, timeframe, count=count)
    if df is None:
        raise HTTPException(status_code=404, detail=f"No OHLCV data for {symbol} {timeframe}")
    # Convert to list of dicts for JSON serialisation (timestamps as ISO strings)
    df["time"] = df["time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return df.to_dict(orient="records")


@router.post("/place")
def place_order(
    body: PlaceOrderRequest,
    client: MT5Client = Depends(get_client),
):
    """
    Place a market order. Validates risk rules before sending.
    Used by the manual confirmation flow and the strategy runner.
    """
    # Check circuit breakers
    _risk_manager = _get_risk_manager()
    allowed, reason = _risk_manager.is_trading_allowed(body.trading_mode)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    # Check concurrent limits
    positions = client.get_open_positions()
    ok, reason = _risk_manager.check_concurrent_limit(body.trading_mode, positions)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)

    # Get symbol info for lot sizing
    sym = client.get_symbol_info(body.symbol)
    if not sym:
        raise HTTPException(status_code=404, detail=f"Symbol not found: {body.symbol}")

    price = client.get_current_price(body.symbol)
    if not price:
        raise HTTPException(status_code=503, detail="Could not fetch current price")

    entry = price["ask"] if body.direction == "BUY" else price["bid"]

    # Validate SL/TP
    valid, err = _risk_manager.validate_sl_tp(body.direction, entry, body.sl, body.tp)
    if not valid:
        raise HTTPException(status_code=400, detail=err)

    # Calculate lot size
    account = client.get_account_info()
    if account is None:
        raise HTTPException(status_code=503, detail="Could not fetch account info")
    lot = _risk_manager.calculate_lot_size(
        account_balance=account["balance"],
        entry_price=entry,
        sl_price=body.sl,
        pip_value=sym["pip_value"],
        pip_size=sym["point"] * 10,   # 1 pip = 10 points for 5-digit brokers
        risk_pct=body.risk_pct,
        min_lot=sym["min_lot"],
        max_lot=sym["max_lot"],
        lot_step=sym["lot_step"],
    )

    om = OrderManager(client)
    prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(
        body.trading_mode, body.trading_mode
    )
    comment = f"{prefix}|{body.comment}"[:31]

    result = om.place_market_order(
        OrderRequest(
            symbol=body.symbol,
            direction=body.direction,
            volume=lot,
            sl=body.sl,
            tp=body.tp,
            comment=comment,
        )
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    # Journal: record manually placed trade
    try:
        from engine.trade_journal import trade_journal
        from engine.account_store import current_mode
        trade_journal.log(
            ticket=result.ticket or 0,
            symbol=body.symbol,
            direction=body.direction,
            volume=lot,
            entry=result.open_price or entry,
            sl=body.sl,
            tp=body.tp,
            profit=None,
            trading_type=body.trading_mode,
            account_mode=current_mode(),
            comment=body.comment,
            event="open",
        )
    except Exception:
        pass

    # Schedule outcome polling so ML/RL modules learn from manual trades
    if result.ticket:
        try:
            import asyncio
            from api.signal_bus import _poll_outcome, bus
            _fake_signal = {
                "symbol":       body.symbol,
                "direction":    body.direction,
                "trading_mode": body.trading_mode,
                "strategy":     body.comment,
                "fill_price":   result.open_price or entry,
                "entry_price":  result.open_price or entry,
                "sl":           body.sl,
                "tp":           body.tp,
                "lot_size":     lot,
                "confidence":   0.5,
            }
            asyncio.get_running_loop().create_task(
                _poll_outcome(ticket=result.ticket, signal=_fake_signal, client=client)
            )
        except Exception:
            pass

    return {
        "ticket":     result.ticket,
        "open_price": result.open_price,
        "volume":     lot,
        "symbol":     body.symbol,
        "direction":  body.direction,
        "sl":         body.sl,
        "tp":         body.tp,
    }


@router.post("/close/{ticket}")
def close_position(
    ticket: int,
    reason: str = Query("manual"),
    client: MT5Client = Depends(get_client),
):
    """Close an open position by ticket number."""
    om = OrderManager(client)
    success = om.close_position(ticket, reason=reason)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to close ticket #{ticket}")
    return {"status": "closed", "ticket": ticket}


@router.post("/close-all")
def close_all(
    symbol: Optional[str] = Query(None),
    client: MT5Client = Depends(get_client),
):
    """Close all open bot positions, optionally filtered by symbol."""
    om = OrderManager(client)
    count = om.close_all_positions(symbol=symbol)
    return {"status": "ok", "closed": count}


@router.patch("/modify/{ticket}")
def modify_position(
    ticket: int,
    body: ModifyPositionRequest,
    client: MT5Client = Depends(get_client),
):
    """Move SL and/or TP on an open position (used by trailing stop logic)."""
    om = OrderManager(client)
    success = om.modify_position(ticket, sl=body.sl, tp=body.tp)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to modify ticket #{ticket}")
    return {"status": "modified", "ticket": ticket}


# ---------------------------------------------------------------------------
# Trade Journal (Phase 9)
# ---------------------------------------------------------------------------

@router.get("/journal")
def get_journal(
    account: str = Query("all",  description="paper | live | all"),
    trading_type: Optional[str] = Query(None, description="scalping | day_trading | swing"),
    event: Optional[str] = Query(None, description="open | close"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Return bot trade journal entries (local JSONL store).
    Supports filtering by account mode, trading type, and event type.
    """
    from engine.trade_journal import trade_journal

    if account not in ("paper", "live", "all"):
        raise HTTPException(status_code=400, detail="account must be 'paper', 'live', or 'all'")

    entries = trade_journal.get(account=account, trading_type=trading_type, event=event, limit=limit)

    # Ensure every close event has its paired open event in the response.
    # Without this, long-lived trades (e.g. swing trades held for days) whose open
    # event is older than `limit` will show the wrong entry time in the dashboard
    # (the dashboard merge falls back to the close event's open_time = logged_at).
    if event != "open":
        open_tickets  = {e["ticket"] for e in entries if e.get("event") == "open"}
        close_tickets = {e["ticket"] for e in entries if e.get("event") == "close"}
        missing = close_tickets - open_tickets
        if missing:
            all_opens = trade_journal.get(account="all", event="open", limit=10_000)
            paired = [e for e in all_opens if e["ticket"] in missing]
            entries = list(entries) + paired
            entries.sort(key=lambda x: x.get("logged_at", ""), reverse=True)

    return {
        "entries": entries,
        "count":   len(entries),
        "filter":  {"account": account, "trading_type": trading_type, "event": event},
    }


@router.get("/journal/stats")
def get_journal_stats(
    account: str = Query("all", description="paper | live | all"),
):
    """Return win/loss/profit summary from the trade journal."""
    from engine.trade_journal import trade_journal

    if account not in ("paper", "live", "all"):
        raise HTTPException(status_code=400, detail="account must be 'paper', 'live', or 'all'")

    paper_stats = trade_journal.stats(account="paper")
    live_stats  = trade_journal.stats(account="live")
    all_stats   = trade_journal.stats(account="all")

    return {
        "paper": paper_stats,
        "live":  live_stats,
        "all":   all_stats,
    }
