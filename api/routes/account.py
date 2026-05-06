"""
Account routes — balance, equity, margin, account switching.
"""

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel

from api.dependencies import get_client
from engine.account_store import current_mode, current_account_login, current_account_type, save_account, _get_all_accounts
from engine.mt5_client import MT5Client

router = APIRouter()


class SwitchAccountRequest(BaseModel):
    login: int
    force: bool = False  # skip open-position guard


@router.get("/")
def get_account(client: MT5Client = Depends(get_client)):
    """Return current account info: balance, equity, margin, mode."""
    info = client.get_account_info()
    if not info:
        raise HTTPException(status_code=500, detail="Failed to fetch account info")
    info["account_type"] = current_account_type()
    return info


@router.get("/mode")
def get_mode():
    """Return the active trading mode without a full account fetch."""
    return {"mode": current_mode(), "login": current_account_login(), "type": current_account_type()}


@router.get("/accounts")
def list_accounts():
    """Return all configured accounts from .env (passwords masked)."""
    accounts = _get_all_accounts()
    safe = []
    for acc in accounts:
        a = dict(acc)
        a.pop("password", None)
        safe.append(a)
    return {
        "accounts": safe,
        "current": current_account_login(),
    }


@router.post("/switch-account")
def switch_account(body: SwitchAccountRequest, client: MT5Client = Depends(get_client)):
    """
    Switch to a specific MT5 account by login number.

    Blocks if there are open positions unless force=true is passed.
    Also pauses the strategy runner while the reconnection happens.
    """
    # Guard: refuse to switch while positions are open (unless forced)
    if not body.force:
        positions = client.get_open_positions() or []
        if positions:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "open_positions",
                    "message": (
                        f"Cannot switch accounts — "
                        f"{len(positions)} open position(s) on current account. "
                        "Close all positions first or pass force=true."
                    ),
                    "open_count": len(positions),
                },
            )

    # Pause runner loop during reconnection
    try:
        from api.runner_loop import pause_runner, resume_runner
        pause_runner()
    except (ImportError, AttributeError):
        pass

    success = client.switch_account(body.login)

    try:
        from api.runner_loop import resume_runner
        resume_runner()
    except (ImportError, AttributeError):
        pass

    if not success:
        raise HTTPException(status_code=500, detail=f"Failed to connect to account {body.login}")

    # After a successful account switch, clear circuit-breakers from previous account
    try:
        from api.main import get_risk_manager
        _rm = get_risk_manager()
        if _rm is not None:
            _rm.reset_for_mode_switch()
            _new_acct = client.get_account_info()
            if _new_acct and _new_acct.get("balance"):
                _rm.update_balance(float(_new_acct["balance"]))
    except Exception:
        pass

    # Reload RL agents for the new account mode
    try:
        from ai.rl_agent import rl_manager
        rl_manager.switch_mode(client.trading_mode)
    except Exception:
        pass

    info = client.get_account_info() or {}
    return {
        "status": "switched",
        "login": body.login,
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