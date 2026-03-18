"""
Account routes — balance, equity, margin, mode switching.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import get_client
from engine.mt5_client import MT5Client

router = APIRouter()


class SwitchModeRequest(BaseModel):
    mode: str  # "paper" or "live"


@router.get("/")
def get_account(client: MT5Client = Depends(get_client)):
    """Return current account info: balance, equity, margin, mode."""
    info = client.get_account_info()
    if not info:
        raise HTTPException(status_code=500, detail="Failed to fetch account info")
    return info


@router.post("/switch-mode")
def switch_mode(body: SwitchModeRequest, client: MT5Client = Depends(get_client)):
    """Switch between paper (demo) and live trading accounts."""
    mode = body.mode.lower()
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    success = client.switch_mode(mode)
    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to switch to {mode} mode")
    return {"status": "switched", "mode": mode}


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
