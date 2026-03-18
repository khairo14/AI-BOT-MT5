"""
Account routes — balance, equity, margin, mode switching.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import get_client
from engine.account_store import current_mode, save_mode
from engine.mt5_client import MT5Client

router = APIRouter()


class SwitchModeRequest(BaseModel):
    mode: str          # "paper" or "live"
    force: bool = False  # skip open-position guard


@router.get("/")
def get_account(client: MT5Client = Depends(get_client)):
    """Return current account info: balance, equity, margin, mode."""
    info = client.get_account_info()
    if not info:
        raise HTTPException(status_code=500, detail="Failed to fetch account info")
    return info


@router.get("/mode")
def get_mode():
    """Return the active trading mode without a full account fetch."""
    return {"mode": current_mode()}


@router.post("/switch-mode")
def switch_mode(body: SwitchModeRequest, client: MT5Client = Depends(get_client)):
    """
    Switch between paper (demo) and live trading accounts.

    Blocks if there are open positions unless force=true is passed.
    Also pauses the strategy runner while the reconnection happens.
    """
    mode = body.mode.lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")

    if mode == client.trading_mode:
        return {"status": "no_change", "mode": mode}

    # Guard: refuse to switch while positions are open (unless forced)
    if not body.force:
        positions = client.get_open_positions() or []
        if positions:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "open_positions",
                    "message": (
                        f"Cannot switch to {mode.upper()} — "
                        f"{len(positions)} open position(s) on current account. "
                        "Close all positions first or pass force=true."
                    ),
                    "open_count": len(positions),
                },
            )

    # Pause runner loop during reconnection
    try:
        from api.runner_loop import pause_runner, resume_runner  # type: ignore[attr-defined]
        pause_runner()
    except (ImportError, AttributeError):
        pass

    success = client.switch_mode(mode)

    try:
        from api.runner_loop import resume_runner  # noqa: F811
        resume_runner()
    except (ImportError, AttributeError):
        pass

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to connect to {mode} MT5 account")

    info = client.get_account_info() or {}
    return {
        "status":  "switched",
        "mode":    mode,
        "account": info,
    }


@router.get("/symbol/{symbol}")
def get_symbol_info(symbol: str, client: MT5Client = Depends(get_client)):
    """Return symbol metadata: spread, pip value, contract size, lot limits."""
    info = client.get_symbol_info(symbol)
    if not info:
        raise HTTPException(status_code=404, detail=f"Symbol not found: {symbol}")
    return info


@router.get("/price/{symbol}")
def get_price(symbol: str, client: MT5Client = Depends(get_client)):
    """Return current bid/ask for a symbol."""
    price = client.get_current_price(symbol)
    if not price:
        raise HTTPException(status_code=404, detail=f"No price data for: {symbol}")
    return price

