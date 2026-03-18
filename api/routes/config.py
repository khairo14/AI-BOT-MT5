"""
Config routes — read and update app/risk/strategy/symbol configuration at runtime.
Changes write to JSON config files and take effect immediately.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"


def _load(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {filename}")
    with open(path) as f:
        return json.load(f)


def _save(filename: str, data: dict) -> None:
    path = CONFIG_DIR / filename
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class PatchRequest(BaseModel):
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

@router.get("/app")
def get_app_config():
    return _load("app.json")


@router.patch("/app")
def update_app_config(body: PatchRequest):
    """Deep-merge partial updates into app.json."""
    current = _load("app.json")
    _deep_merge(current, body.data)
    _save("app.json", current)
    return current


# ---------------------------------------------------------------------------
# Risk config
# ---------------------------------------------------------------------------

@router.get("/risk")
def get_risk_config():
    return _load("risk.json")


@router.patch("/risk")
def update_risk_config(body: PatchRequest):
    """Deep-merge partial updates into risk.json."""
    current = _load("risk.json")
    _deep_merge(current, body.data)
    _save("risk.json", current)
    # Signal the risk manager to reload
    try:
        from api.routes.trades import _risk_manager
        _risk_manager.reload_config()
    except Exception:
        pass
    return current


# ---------------------------------------------------------------------------
# Symbols config
# ---------------------------------------------------------------------------

@router.get("/symbols")
def get_symbols_config():
    return _load("symbols.json")


@router.patch("/symbols")
def update_symbols_config(body: PatchRequest):
    """Update symbol enable/disable flags."""
    current = _load("symbols.json")
    _deep_merge(current, body.data)
    _save("symbols.json", current)
    return current


# ---------------------------------------------------------------------------
# Strategies config
# ---------------------------------------------------------------------------

@router.get("/strategies")
def get_strategies_config():
    return _load("strategies.json")


@router.patch("/strategies")
def update_strategies_config(body: PatchRequest):
    """Update strategy parameters or active strategy lists."""
    current = _load("strategies.json")
    _deep_merge(current, body.data)
    _save("strategies.json", current)
    return current


# ---------------------------------------------------------------------------
# Execution mode per trading type
# ---------------------------------------------------------------------------

@router.get("/execution-mode")
def get_execution_mode():
    app = _load("app.json")
    return app.get("execution_mode", {})


@router.patch("/execution-mode/{trading_mode}")
def set_execution_mode(trading_mode: str, mode: str):
    """
    Set execution mode for a trading type.
    trading_mode: "scalping" | "day_trading" | "swing"
    mode: "auto" | "manual"
    """
    if mode not in ("auto", "manual"):
        raise HTTPException(status_code=400, detail="mode must be 'auto' or 'manual'")
    if trading_mode not in ("scalping", "day_trading", "swing"):
        raise HTTPException(status_code=400, detail="invalid trading_mode")
    app = _load("app.json")
    app.setdefault("execution_mode", {})[trading_mode] = mode
    _save("app.json", app)
    return {"trading_mode": trading_mode, "execution_mode": mode}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, updates: dict) -> None:
    """Recursively merge `updates` into `base` in-place."""
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
