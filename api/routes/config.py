"""
Config routes — read and update app/risk/strategy/symbol configuration at runtime.
Changes write to JSON config files and take effect immediately.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.dependencies import get_client
from engine.mt5_client import MT5Client

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

# ── Schema definitions ────────────────────────────────────────────────────────
# Each schema entry: field_path (dot-separated) → (type, min, max, allowed_values)
# type: "number" | "bool" | "str" | "str_enum"
# min/max only used for "number"; allowed_values only for "str_enum".
# Nested paths like "drawdown.daily_limit_pct" match inside nested dicts.
_RISK_SCHEMA: dict[str, tuple] = {
    "risk_per_trade_pct":         ("number", 0.01, 10.0, None),
    "max_risk_per_trade_pct":     ("number", 0.01, 20.0, None),
    "risk_reward_min":            ("number", 0.5,  10.0, None),
    "daily_limit_pct":            ("number", 0.1,  50.0, None),
    "weekly_limit_pct":           ("number", 0.1, 100.0, None),
    "max_consecutive_losses":     ("number", 1.0,  50.0, None),
    "consecutive_loss_pause_hours":("number",0.0, 168.0, None),
    "pause_minutes_before":       ("number", 0.0, 120.0, None),
    "pause_minutes_after":        ("number", 0.0,  60.0, None),
}

_APP_SCHEMA: dict[str, tuple] = {
    "confidence_threshold":       ("number",  0.0, 100.0, None),
    "max_correlated_positions":   ("number",  1.0,  10.0, None),
    "lstm":                       ("number",  0.0,   1.0, None),
    "rr":                         ("number",  0.0,   1.0, None),
    "trend":                      ("number",  0.0,   1.0, None),
    "volume":                     ("number",  0.0,   1.0, None),
}

_SCANNER_SCHEMA: dict[str, tuple] = {
    "enabled": ("bool", None, None, None),
}

# Max symbols per scanner mode — also enforced in update_scanner_config
_SCANNER_MAX = {"scalping": 7, "day_trading": 15, "swing": 18}


def _validate_schema(data: dict, schema: dict, path: str = "") -> None:
    """
    Recursively validate a dict against a flat schema.
    Schema keys are matched against leaf-level field names anywhere in the tree.
    Raises HTTPException(422) with a descriptive message on first violation.
    """
    for key, value in data.items():
        full_path = f"{path}.{key}" if path else key
        if key in schema:
            typ, lo, hi, choices = schema[key]
            if typ == "number":
                if not isinstance(value, (int, float)):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{full_path}' must be a number, got {type(value).__name__!r}",
                    )
                if lo is not None and value < lo:
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{full_path}' = {value} is below minimum {lo}",
                    )
                if hi is not None and value > hi:
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{full_path}' = {value} exceeds maximum {hi}",
                    )
            elif typ == "bool":
                if not isinstance(value, bool):
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{full_path}' must be true/false, got {type(value).__name__!r}",
                    )
            elif typ == "str_enum" and choices:
                if value not in choices:
                    raise HTTPException(
                        status_code=422,
                        detail=f"'{full_path}' must be one of {choices}, got {value!r}",
                    )
        if isinstance(value, dict):
            _validate_schema(value, schema, full_path)


def _validate_numeric_fields(data: dict) -> None:
    """Recursively check that known numeric fields contain actual numbers.
    Kept for backward compatibility — full schema validation is done per-endpoint."""
    for key, value in data.items():
        if key in _NUMERIC_FIELDS and not isinstance(value, (int, float)):
            raise HTTPException(
                status_code=422,
                detail=f"Field '{key}' must be a number, got {type(value).__name__!r}",
            )
        if isinstance(value, dict):
            _validate_numeric_fields(value)


def _load(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Config file not found: {filename}")
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Config file is corrupted ({filename}): {exc}")


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
    """Deep-merge partial updates into app.json with schema validation."""
    _validate_numeric_fields(body.data)
    _validate_schema(body.data, _APP_SCHEMA)
    # Validate scorer weight sums if present — must sum to ≈1.0
    for weight_key in ("scorer_weights", "scalping_scorer_weights", "swing_scorer_weights"):
        wdata = body.data.get("ai", {}).get(weight_key)
        if wdata and isinstance(wdata, dict):
            total = sum(float(v) for v in wdata.values() if isinstance(v, (int, float)))
            if total > 0 and abs(total - 1.0) > 0.05:
                raise HTTPException(
                    status_code=422,
                    detail=f"ai.{weight_key} weights sum to {total:.3f}; must sum to 1.0 (±0.05)",
                )
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
    """Deep-merge partial updates into risk.json with schema validation."""
    _validate_numeric_fields(body.data)
    _validate_schema(body.data, _RISK_SCHEMA)
    # Extra cross-field check: daily limit must be <= weekly limit
    dd = body.data.get("drawdown", {})
    daily  = dd.get("daily_limit_pct")
    weekly = dd.get("weekly_limit_pct")
    if daily is not None and weekly is not None and daily > weekly:
        raise HTTPException(
            status_code=422,
            detail=f"drawdown.daily_limit_pct ({daily}) cannot exceed weekly_limit_pct ({weekly})",
        )
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
    # Validate each mode entry is a list of dicts with required keys
    for mode, entries in body.data.items():
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise HTTPException(
                    status_code=422,
                    detail=f"symbols.{mode}[{i}] must be an object with 'symbol' and 'enabled' keys",
                )
            if "symbol" in entry and not isinstance(entry["symbol"], str):
                raise HTTPException(
                    status_code=422,
                    detail=f"symbols.{mode}[{i}].symbol must be a string",
                )
            if "enabled" in entry and not isinstance(entry["enabled"], bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"symbols.{mode}[{i}].enabled must be true or false",
                )
    current = _load("symbols.json")
    _deep_merge(current, body.data)
    _save("symbols.json", current)
    return current


@router.get("/available-symbols")
def get_available_symbols(client: MT5Client = Depends(get_client)):
    """Return all symbols available on the connected broker (XM via MT5),
    grouped by category. The list is live from mt5.symbols_get() so it only
    contains instruments XM actually offers on this account type."""
    raw = client.get_all_symbols()
    if not raw:
        return {"symbols": [], "grouped": {}}

    # Category detection — ordered most-specific to least-specific
    def _category(name: str) -> str:
        u = name.upper()
        forex_ccys = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
                      "SGD", "HKD", "NOK", "SEK", "DKK", "MXN", "ZAR", "TRY",
                      "PLN", "CZK", "HUF", "CNH"]
        is_forex = (
            len(name) == 6
            and any(u.startswith(c) for c in forex_ccys)
            and any(u.endswith(c) for c in forex_ccys)
        )
        if is_forex:
            majors = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"}
            return "forex_major" if u in majors else "forex_minor"
        crypto_tokens = ["BTC", "ETH", "XRP", "SOL", "ADA", "BNB", "DOGE",
                         "MATIC", "DOT", "AVAX", "LINK", "LTC", "XLM", "UNI",
                         "ATOM", "FIL", "TRX", "ALGO", "VET", "FTM"]
        if any(tok in u for tok in crypto_tokens):
            return "crypto"
        if any(x in u for x in ["GOLD", "XAU", "SILVER", "XAG", "PLAT", "PALL"]):
            return "precious_metals"
        if any(x in u for x in ["OIL", "BRENT", "WTI", "NGAS", "GAS", "COCOA",
                                  "COFFEE", "CORN", "WHEAT", "SUGAR", "COTTON"]):
            return "commodities"
        if any(x in u for x in ["US100", "US30", "US500", "SPX", "NAS", "DOW",
                                  "GER", "UK100", "DAX", "CAC", "NIKKEI", "AUS",
                                  "HK50", "JP225", "STOXX", "FTSE", "SPI"]):
            return "indices"
        # Everything else (stocks, ETFs, bonds)
        return "stocks"

    grouped: dict[str, list[str]] = {}
    for sym in raw:
        name = sym.name
        cat = _category(name)
        grouped.setdefault(cat, [])
        if name not in grouped[cat]:
            grouped[cat].append(name)

    # Sort each category alphabetically
    for cat in grouped:
        grouped[cat].sort()

    flat = sorted({sym.name for sym in raw})
    return {"symbols": flat, "grouped": grouped}


# ---------------------------------------------------------------------------
# Strategies config
# ---------------------------------------------------------------------------

@router.get("/strategies")
def get_strategies_config():
    return _load("strategies.json")


@router.patch("/strategies")
def update_strategies_config(body: PatchRequest):
    """Update strategy parameters or active strategy lists with validation."""
    _validate_numeric_fields(body.data)
    # Validate active_strategies lists contain only known strategy names
    _KNOWN_STRATEGIES = {
        "ema_scalp", "bb_squeeze", "vwap_reversion",
        "macd_ema_trend", "sr_breakout", "rsi_divergence",
        "ema_trend_rider", "fibonacci_rsi", "weekly_breakout",
    }
    for mode_key, mode_val in body.data.items():
        if isinstance(mode_val, dict):
            active = mode_val.get("active_strategies")
            if active is not None:
                if not isinstance(active, list):
                    raise HTTPException(
                        status_code=422,
                        detail=f"strategies.{mode_key}.active_strategies must be a list",
                    )
                unknown = [s for s in active if s not in _KNOWN_STRATEGIES]
                if unknown:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Unknown strategy name(s) in {mode_key}: {unknown}. "
                               f"Valid names: {sorted(_KNOWN_STRATEGIES)}",
                    )
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
    """Update scanner settings. Enforces per-mode symbol limits and type checks."""
    _validate_numeric_fields(body.data)
    _validate_schema(body.data, _SCANNER_SCHEMA)
    # Validate per-mode symbol lists
    for mode, limit in _SCANNER_MAX.items():
        mode_data = body.data.get(mode)
        if mode_data is None:
            continue
        if not isinstance(mode_data, dict):
            raise HTTPException(
                status_code=422,
                detail=f"scanner.{mode} must be an object",
            )
        syms = mode_data.get("symbols")
        if syms is not None:
            if not isinstance(syms, list):
                raise HTTPException(
                    status_code=422,
                    detail=f"scanner.{mode}.symbols must be a list of symbol strings",
                )
            if not all(isinstance(s, str) for s in syms):
                raise HTTPException(
                    status_code=422,
                    detail=f"scanner.{mode}.symbols must contain only strings",
                )
    current = _load("scanner.json")
    _deep_merge(current, body.data)
    for m, limit in _SCANNER_MAX.items():
        if m in current and isinstance(current[m].get("symbols"), list):
            current[m]["symbols"] = current[m]["symbols"][:limit]
    _save("scanner.json", current)
    return current


# ---------------------------------------------------------------------------
# Config validation endpoint — check all files are well-formed
# ---------------------------------------------------------------------------

@router.get("/validate")
def validate_all_configs():
    """
    Read all config files and report any JSON parse errors or missing files.
    Returns a dict of {filename: "ok" | error_message} for each file.
    Useful for diagnosing silent corruption after a bad PATCH or manual edit.
    """
    _FILES = ["app.json", "risk.json", "symbols.json", "strategies.json",
              "scanner.json", "account_mode.json"]
    results = {}
    for fname in _FILES:
        path = CONFIG_DIR / fname
        if not path.exists():
            results[fname] = "missing"
            continue
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
            results[fname] = "ok"
        except json.JSONDecodeError as exc:
            results[fname] = f"JSON error: {exc}"
        except Exception as exc:
            results[fname] = f"read error: {exc}"
    all_ok = all(v == "ok" for v in results.values())
    return {"all_ok": all_ok, "files": results}


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
