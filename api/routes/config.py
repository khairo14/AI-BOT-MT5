"""
Config routes — read and update app/risk/strategy/symbol configuration at runtime.
Changes write to JSON config files and take effect immediately.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

CONFIG_DIR = Path(__file__).parent.parent.parent / "config"

# Numeric fields that must stay numeric — prevent bad PATCH values from
# crashing lot calculation or risk checks at runtime.
_NUMERIC_FIELDS = frozenset({
    "risk_per_trade_pct", "max_risk_per_trade_pct", "risk_reward_min",
    "daily_limit_pct", "weekly_limit_pct", "max_consecutive_losses",
    "consecutive_loss_pause_hours", "confidence_threshold",
    "min_lot", "max_lot", "lot_step", "risk_pct",
})


def _load(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {filename}")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Config file is corrupted ({filename}): {exc}")


def _validate_numeric_fields(data: dict) -> None:
    """Recursively check that known numeric fields contain actual numbers."""
    for key, value in data.items():
        if key in _NUMERIC_FIELDS and not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=422,
                detail=f"Field '{key}' must be a number, got {type(value).__name__!r}",
            )
        if isinstance(value, dict):
            _validate_numeric_fields(value)


def _save(filename: str, data: dict) -> None:
    """Atomic write: serialise to a temp file then rename over the target.
    Guarantees the config file is never left empty on a crash or serialisation error."""
    path = CONFIG_DIR / filename
    serialised = json.dumps(data, indent=2)
    # Write to a temp file in the same directory (same filesystem → atomic rename)
    fd, tmp_path = tempfile.mkstemp(dir=str(CONFIG_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(serialised)
        os.replace(tmp_path, path)   # atomic on POSIX; near-atomic on Windows
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class PatchRequest(BaseModel):
    data: dict[str, Any]


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------

@router.get("/app")
def get_app_config():
    data = _load("app.json")
    # Mask broker account numbers so they are not exposed over the API
    if "accounts" in data and isinstance(data["accounts"], dict):
        for acct in data["accounts"].values():
            if isinstance(acct, dict) and "login" in acct:
                acct["login"] = "***"
    return data


@router.patch("/app")
def update_app_config(body: PatchRequest):
    """Deep-merge partial updates into app.json."""
    _validate_numeric_fields(body.data)
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
    _validate_numeric_fields(body.data)
    current = _load("risk.json")
    _deep_merge(current, body.data)
    _save("risk.json", current)
    # Signal the risk manager to reload
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is not None:
            rm.reload_config()
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
    _validate_numeric_fields(body.data)
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
    _validate_numeric_fields(body.data)
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
# Scanner config
# ---------------------------------------------------------------------------

@router.get("/scanner")
def get_scanner_config():
    """Return per-mode scanner settings (enabled, symbols, timeframe)."""
    return _load("scanner.json")


@router.patch("/scanner")
def update_scanner_config(body: PatchRequest):
    """Update scanner settings. Enforces per-mode symbol limits."""
    _validate_numeric_fields(body.data)
    _SCANNER_MAX = {"scalping": 5, "day_trading": 10, "swing": 14}
    current = _load("scanner.json")
    _deep_merge(current, body.data)
    for m, limit in _SCANNER_MAX.items():
        if m in current and isinstance(current[m].get("symbols"), list):
            current[m]["symbols"] = current[m]["symbols"][:limit]
    _save("scanner.json", current)
    return current


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
