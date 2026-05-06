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
    direction: str            # "buy"/"sell" or "BUY"/"SELL" — normalised in handler
    # Absolute price levels (used by strategy runner)
    sl: Optional[float] = None
    tp: Optional[float] = None
    # Pip-relative levels (used by dashboard TradePanel)
    sl_pips: Optional[float] = None
    tp_pips: Optional[float] = None
    risk_pct: Optional[float] = None
    trading_mode: Optional[str] = None   # "scalping" | "day_trading" | "swing"
    mode: Optional[str] = None           # alias for trading_mode (frontend)
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
    positions = client.get_open_positions(symbol=symbol)
    
    # Enrich positions with trailing stop status
    try:
        from api.runner_loop import _trailing_stop_manager
        if _trailing_stop_manager:
            for pos in positions:
                trailing_status = _trailing_stop_manager.get_trailing_status(pos["ticket"])
                if trailing_status:
                    pos["trailing"] = {
                        "active": True,
                        "pips_trailed": trailing_status["pips_trailed"],
                        "last_trail_at": trailing_status["last_trail_at"],
                    }
                else:
                    pos["trailing"] = {"active": False}
    except Exception:
        pass  # Trailing stop data optional
    
    return positions


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
    # Normalise direction and resolve trading_mode/mode alias
    direction = body.direction.upper()
    trading_mode = body.trading_mode or body.mode
    if not trading_mode:
        raise HTTPException(status_code=422, detail="trading_mode (or mode) is required")

    # Check circuit breakers
    _risk_manager = _get_risk_manager()
    allowed, reason = _risk_manager.is_trading_allowed(trading_mode)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)

    # Check concurrent limits
    positions = client.get_open_positions()
    ok, reason = _risk_manager.check_concurrent_limit(trading_mode, positions)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)

    # Get symbol info for lot sizing
    sym = client.get_symbol_info(body.symbol)
    if not sym:
        raise HTTPException(status_code=404, detail=f"Symbol not found: {body.symbol}")

    price = client.get_current_price(body.symbol)
    if not price:
        raise HTTPException(status_code=503, detail="Could not fetch current price")

    entry = price["ask"] if direction == "BUY" else price["bid"]

    # Compute absolute SL/TP from pips when the frontend sends pip-relative values
    pip_size = sym["point"] * (10 if sym["digits"] in (3, 5) else 1)
    digits = sym["digits"]
    sl = body.sl
    tp = body.tp
    if sl is None and body.sl_pips is not None:
        sl = round(entry - body.sl_pips * pip_size if direction == "BUY" else entry + body.sl_pips * pip_size, digits)
    if tp is None and body.tp_pips is not None:
        tp = round(entry + body.tp_pips * pip_size if direction == "BUY" else entry - body.tp_pips * pip_size, digits)

    if sl is None:
        raise HTTPException(status_code=422, detail="sl or sl_pips is required")

    # Validate SL/TP
    valid, err = _risk_manager.validate_sl_tp(direction, entry, sl, tp, trading_mode)
    if not valid:
        raise HTTPException(status_code=400, detail=err)

    # Calculate lot size
    account = client.get_account_info()
    if account is None:
        raise HTTPException(status_code=503, detail="Could not fetch account info")
    lot = _risk_manager.calculate_lot_size(
        account_balance=account["balance"],
        entry_price=entry,
        sl_price=sl,
        pip_value=sym["pip_value"],
        pip_size=pip_size,
        risk_pct=body.risk_pct,
        min_lot=sym["min_lot"],
        max_lot=sym["max_lot"],
        lot_step=sym["lot_step"],
    )

    om = OrderManager(client)
    prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(
        trading_mode, trading_mode
    )
    comment = f"{prefix}|{body.comment}"[:31]

    result = om.place_market_order(
        OrderRequest(
            symbol=body.symbol,
            direction=direction,
            volume=lot,
            sl=sl,
            tp=tp,
            comment=comment,
        )
    )

    if not result.success:
        raise HTTPException(status_code=500, detail=result.error)

    # Journal: record manually placed trade
    try:
        from engine.trade_journal import trade_journal
        from engine.account_store import current_mode, current_account_login
        trade_journal.log(
            ticket=result.ticket or 0,
            symbol=body.symbol,
            direction=direction,
            volume=lot,
            entry=result.open_price or entry,
            sl=sl,
            tp=tp,
            profit=None,
            trading_type=trading_mode,
            account_mode=current_mode(),
            comment=body.comment,
            event="open",
            account_login=current_account_login(),
        )
    except Exception:
        pass

    # Schedule outcome polling so ML/RL modules learn from manual trades
    if result.ticket:
        try:
            import asyncio
            from api.signal_bus import _poll_outcome
            _fake_signal = {
                "symbol":       body.symbol,
                "direction":    direction,
                "trading_mode": trading_mode,
                "strategy":     body.comment,
                "fill_price":   result.open_price or entry,
                "entry_price":  result.open_price or entry,
                "sl":           sl,
                "tp":           tp,
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
    # Get position info before closing for notification
    import MetaTrader5 as mt5
    with client._lock:
        positions = mt5.positions_get(ticket=ticket)
    pos_info = positions[0] if positions else None
    
    om = OrderManager(client)
    success = om.close_position(ticket, reason=reason)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to close ticket #{ticket}")
    
    # Broadcast position_closed notification
    if pos_info:
        try:
            from api.websocket.feed import manager as _ws_manager
            import asyncio
            asyncio.create_task(_ws_manager.broadcast_alert({
                "type": "position_closed",
                "symbol": pos_info.symbol,
                "profit": round(pos_info.profit, 2),
                "strategy": pos_info.comment if pos_info.comment else "",
            }))
        except Exception:
            pass  # WebSocket broadcast is optional
    
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


@router.get("/trailing-stops")
def get_trailing_stops():
    """Get trailing stop status for all tracked positions."""
    try:
        from api.runner_loop import _trailing_stop_manager
        if not _trailing_stop_manager:
            return {"enabled": False, "positions": []}
        
        statuses = _trailing_stop_manager.get_all_trailing_status()
        return {
            "enabled": True,
            "positions": statuses,
            "count": len(statuses),
        }
    except Exception as exc:
        return {"enabled": False, "error": str(exc), "positions": []}


# ---------------------------------------------------------------------------
# Trade Journal (Phase 9) - UPDATED with account_login filter
# ---------------------------------------------------------------------------

@router.get("/journal")
def get_journal(
    account: str = Query("all", description="paper | live | all"),
    account_login: Optional[int] = Query(None, description="Filter by specific MT5 account login number"),
    trading_type: Optional[str] = Query(None, description="scalping | day_trading | swing"),
    event: Optional[str] = Query(None, description="open | close"),
    limit: int = Query(100, ge=1, le=1000),
):
    """
    Return bot trade journal entries (local JSONL store).
    Supports filtering by account mode, specific account login, trading type, and event type.
    """
    from engine.trade_journal import trade_journal

    if account not in ("paper", "live", "all"):
        raise HTTPException(status_code=400, detail="account must be 'paper', 'live', or 'all'")

    # If account_login is provided, use it (overrides account mode filter)
    if account_login is not None:
        entries = trade_journal.get(
            account="all",  # Don't filter by mode when using specific login
            trading_type=trading_type,
            event=event,
            limit=limit,
            account_login=account_login,
        )
    else:
        entries = trade_journal.get(
            account=account,
            trading_type=trading_type,
            event=event,
            limit=limit,
        )

    # Ensure every close event has its paired open event in the response.
    if event != "open":
        open_tickets = {e["ticket"] for e in entries if e.get("event") == "open"}
        close_tickets = {e["ticket"] for e in entries if e.get("event") == "close"}
        missing = close_tickets - open_tickets
        if missing:
            if account_login is not None:
                all_opens = trade_journal.get(account="all", event="open", limit=10_000, account_login=account_login)
            else:
                all_opens = trade_journal.get(account=account, event="open", limit=10_000)
            paired = [e for e in all_opens if e["ticket"] in missing]
            entries = list(entries) + paired
            entries.sort(key=lambda x: x.get("logged_at", ""), reverse=True)

    return {
        "entries": entries,
        "count":   len(entries),
        "filter":  {"account": account, "account_login": account_login, "trading_type": trading_type, "event": event},
    }


@router.get("/journal/stats")
def get_journal_stats(
    account: str = Query("all", description="paper | live | all"),
    account_login: Optional[int] = Query(None, description="Filter by specific MT5 account login number"),
):
    """Return win/loss/profit summary from the trade journal."""
    from engine.trade_journal import trade_journal

    if account not in ("paper", "live", "all"):
        raise HTTPException(status_code=400, detail="account must be 'paper', 'live', or 'all'")

    # If account_login is provided, use it for all stats
    if account_login is not None:
        paper_stats = trade_journal.stats(account="paper", account_login=account_login)
        live_stats = trade_journal.stats(account="live", account_login=account_login)
        all_stats = trade_journal.stats(account="all", account_login=account_login)
    else:
        paper_stats = trade_journal.stats(account="paper")
        live_stats = trade_journal.stats(account="live")
        all_stats = trade_journal.stats(account="all")

    return {
        "paper": paper_stats,
        "live":  live_stats,
        "all":   all_stats,
    }


@router.get("/journal/export")
def export_journal(
    account: str = Query("all", description="paper | live | all"),
    account_login: Optional[int] = Query(None, description="Filter by specific MT5 account login number"),
    trading_type: Optional[str] = Query(None, description="scalping | day_trading | swing"),
    days: Optional[int] = Query(None, description="Filter trades from last N days"),
    format: str = Query("csv", description="csv | excel"),
):
    """
    Export trade journal as CSV or Excel file.
    Returns file download with proper headers for browser download prompt.
    """
    import csv
    import io
    from datetime import datetime, timedelta, timezone
    from fastapi.responses import StreamingResponse
    
    from engine.trade_journal import trade_journal

    if account not in ("paper", "live", "all"):
        raise HTTPException(status_code=400, detail="account must be 'paper', 'live', or 'all'")
    
    if format not in ("csv", "excel"):
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'excel'")

    # Get all closed trades
    if account_login is not None:
        entries = trade_journal.get(
            account="all",
            trading_type=trading_type,
            event=None,
            limit=10_000,
            account_login=account_login,
        )
    else:
        entries = trade_journal.get(
            account=account,
            trading_type=trading_type,
            event=None,
            limit=10_000,
        )
    
    # Filter by date if specified
    if days:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
        entries = [
            e for e in entries
            if datetime.fromisoformat(e.get("logged_at", "").replace("Z", "+00:00")) >= cutoff
        ]
    
    # Group by ticket to match open/close pairs
    trades_by_ticket = {}
    for entry in entries:
        ticket = entry.get("ticket")
        if ticket not in trades_by_ticket:
            trades_by_ticket[ticket] = {"open": None, "close": None}
        if entry.get("event") == "open":
            trades_by_ticket[ticket]["open"] = entry
        elif entry.get("event") == "close":
            trades_by_ticket[ticket]["close"] = entry
    
    # Build export rows (only closed trades with both open and close)
    export_rows = []
    for ticket, events in trades_by_ticket.items():
        if not events["open"] or not events["close"]:
            continue  # Skip incomplete trades
        
        open_evt = events["open"]
        close_evt = events["close"]
        
        open_time = open_evt.get("logged_at", "")
        close_time = close_evt.get("logged_at", "")
        
        # Calculate duration
        try:
            open_dt = datetime.fromisoformat(open_time.replace("Z", "+00:00"))
            close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            duration_hours = (close_dt - open_dt).total_seconds() / 3600
        except Exception:
            duration_hours = 0.0
        
        export_rows.append({
            "Ticket": ticket,
            "Open Time": open_time,
            "Close Time": close_time,
            "Duration (hours)": round(duration_hours, 2),
            "Symbol": close_evt.get("symbol", ""),
            "Direction": open_evt.get("direction", "").upper(),
            "Trading Type": open_evt.get("trading_type", ""),
            "Strategy": open_evt.get("strategy", ""),
            "Volume": close_evt.get("volume", 0.0),
            "Entry Price": open_evt.get("entry", 0.0),
            "Exit Price": close_evt.get("entry", 0.0),
            "Stop Loss": open_evt.get("sl", 0.0),
            "Take Profit": open_evt.get("tp", 0.0),
            "Profit": close_evt.get("profit", 0.0),
            "Pips": close_evt.get("profit_pips", 0.0),
            "Confidence": open_evt.get("confidence", 0.0),
            "Account Login": close_evt.get("account_login", ""),
            "Account Mode": close_evt.get("account_mode", ""),
            "Comment": close_evt.get("comment", ""),
        })
    
    # Sort by close time descending
    export_rows.sort(key=lambda x: x["Close Time"], reverse=True)
    
    if format == "csv":
        output = io.StringIO()
        if export_rows:
            writer = csv.DictWriter(output, fieldnames=export_rows[0].keys())
            writer.writeheader()
            writer.writerows(export_rows)
        
        output.seek(0)
        filename = f"trades_{account}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
    else:  # excel
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill, Alignment
            from openpyxl.utils import get_column_letter
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="openpyxl not installed. Install with: pip install openpyxl"
            )
        
        wb = Workbook()
        ws = wb.active
        ws.title = "Trade Journal"
        
        if export_rows:
            headers = list(export_rows[0].keys())
            ws.append(headers)
            
            header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
            header_font = Font(bold=True, color="FFFFFF")
            for cell in ws[1]:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")
            
            for row in export_rows:
                ws.append(list(row.values()))
            
            # Color-code profit column
            profit_col_idx = headers.index("Profit") + 1
            green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
            red_fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
            
            for row_idx in range(2, ws.max_row + 1):
                cell = ws.cell(row=row_idx, column=profit_col_idx)
                try:
                    if float(cell.value or 0) > 0:
                        cell.fill = green_fill
                    elif float(cell.value or 0) < 0:
                        cell.fill = red_fill
                except (ValueError, TypeError):
                    pass
            
            # Auto-size columns
            for col_idx, col in enumerate(ws.columns, start=1):
                max_length = 0
                column_letter = get_column_letter(col_idx)
                for cell in col:
                    try:
                        if len(str(cell.value)) > max_length:
                            max_length = len(str(cell.value))
                    except:
                        pass
                adjusted_width = min(max_length + 2, 50)
                ws.column_dimensions[column_letter].width = adjusted_width
        
        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        
        filename = f"trades_{account}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )