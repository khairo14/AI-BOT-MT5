"""
Account routes — balance, equity, margin, mode switching.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
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

    # After a successful mode switch, clear any circuit-breakers that were set
    # in the previous mode (e.g. drawdown losses on paper shouldn't block live)
    try:
        from api.main import get_risk_manager
        _rm = get_risk_manager()
        if _rm is not None:
            _rm.reset_for_mode_switch()
            # LIVE-1: re-seed the new account's balance immediately so daily drawdown
            # protection is active from the first trade, not only after the first close.
            # main.py seeds on startup but does NOT re-seed on mid-session mode switches.
            _new_acct = client.get_account_info()
            if _new_acct and _new_acct.get("balance"):
                _rm.update_balance(float(_new_acct["balance"]))
    except Exception:
        pass

    # Reload RL agents for the new account mode (C-2 fix)
    try:
        from ai.rl_agent import rl_manager
        rl_manager.switch_mode(mode)
    except Exception:
        pass

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


@router.post("/reconnect")
def reconnect_mt5():
    """Attempt to reconnect to MT5 after a dropped connection."""
    from api.main import get_mt5_client
    client = get_mt5_client()
    if client is None:
        raise HTTPException(status_code=503, detail="MT5 client not initialized — restart the bot.")
    success = client.reconnect()
    if not success:
        raise HTTPException(status_code=503, detail="MT5 reconnect failed — check terminal is running.")
    # Re-wire SignalBus so order execution continues to work after reconnect
    try:
        from engine.order_manager import OrderManager
        from api.signal_bus import bus
        _om = OrderManager(client)
        bus.init(client, _om)
    except Exception as _e:
        logger.warning(f"SignalBus re-init after reconnect failed: {_e}")
    return {"status": "reconnected", "connected": True}


@router.get("/price/{symbol}")
def get_price(symbol: str, client: MT5Client = Depends(get_client)):
    """Return current bid/ask for a symbol."""
    price = client.get_current_price(symbol)
    if not price:
        raise HTTPException(status_code=404, detail=f"No price data for: {symbol}")
    return price

