"""
Risk management status endpoints — Phase 8.

GET /risk/status    — risk manager state (drawdown, halted modes, circuit breaker)
GET /risk/news      — current news filter status + upcoming events
GET /risk/sessions  — session filter open/closed status for all configured symbols
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter()

_SYMBOLS_PATH = Path(__file__).parent.parent.parent / "config" / "symbols.json"


def _detect_category_from_symbol(symbol: str) -> str:
    """
    Dynamically detect asset category from symbol name (fallback when symbols.json missing).
    """
    clean = symbol.upper().rstrip("#+*!._-")
    
    # Crypto
    crypto = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "LTC", "BNB", "XLM", "ETC", "GRT"}
    if any(c in clean for c in crypto):
        return "crypto"
    
    # US Stocks
    stocks = {"TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NFLX", "AMD", "INTC", "ADV"}
    if any(stk in clean for stk in stocks):
        return "stock"
    
    # US Indices
    us_indices = {"US30", "US100", "US500", "SPX", "NAS", "NDX"}
    if any(idx in clean for idx in us_indices):
        return "us_index"
    
    # EU Indices
    eu_indices = {"GER40", "DAX", "UK100", "FRA40", "EU50"}
    if any(idx in clean for idx in eu_indices):
        return "eu_index"
    
    # Commodities
    commodities = {"GOLD", "XAU", "SILVER", "XAG", "OIL", "BRENT", "WTI", "NGAS"}
    if any(cmd in clean for cmd in commodities):
        return "commodity"
    
    # Forex (6 letters)
    letters = ''.join(c for c in clean if c.isalpha())
    if len(letters) == 6:
        currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD"}
        first_three = letters[:3]
        last_three = letters[3:]
        if first_three in currencies and last_three in currencies:
            return "forex"
    
    return "forex"


def _all_symbols() -> list[tuple[str, str]]:
    """
    Return [(symbol, category), ...] for symbols.
    Uses symbols.json if available, otherwise returns empty list with warning.
    Session filter will fall back to dynamic detection.
    """
    if not _SYMBOLS_PATH.exists():
        # Return empty list - session_filter will detect categories dynamically
        return []
    
    try:
        with open(_SYMBOLS_PATH) as f:
            data = json.load(f)
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for _mode, symbols in data.items():
            for s in symbols:
                sym = s.get("symbol") if isinstance(s, dict) else s
                if sym and sym not in seen:
                    seen.add(sym)
                    cat = s.get("category", "forex") if isinstance(s, dict) else "forex"
                    result.append((sym, cat))
        return result
    except Exception:
        return []


# ---------------------------------------------------------------------------
# GET /risk/status
# ---------------------------------------------------------------------------

@router.get("/status")
def risk_status():
    """
    Returns the current state of the RiskManager.

    Fields:
      - daily_halted:      True if daily drawdown limit hit
      - weekly_halted:     True if weekly drawdown limit hit
      - consecutive_losses: Per-mode streak of consecutive losses
      - paused_modes:      Modes with auto-pause active
      - day_start_balance: Balance at start of current trading day
      - week_start_balance: Balance at start of current week
      - config:            Active risk thresholds from risk.json
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    status = rm.get_status()
    status["config"] = {
        "risk_per_trade_pct":     rm._config.get("risk_per_trade_pct"),
        "max_risk_per_trade_pct": rm._config.get("max_risk_per_trade_pct"),
    }
    return status


# ---------------------------------------------------------------------------
# GET /risk/news
# ---------------------------------------------------------------------------

@router.get("/news")
def news_status():
    """
    Returns the state of the NewsFilter + upcoming market events.

    Fields:
      - enabled:       Whether the news filter is active (per risk.json)
      - cache_age_s:   Seconds since last ForexFactory fetch
      - next_events:   Up to 10 upcoming high-impact events
    """
    from engine.news_filter import news_filter

    cache_info = news_filter.status()
    upcoming   = news_filter.next_events(
        currencies=["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"],
        n=10,
    )

    return {
        **cache_info,
        "next_events": upcoming,
    }


# ---------------------------------------------------------------------------
# GET /risk/sessions
# ---------------------------------------------------------------------------

@router.get("/sessions")
def sessions_status():
    """
    Returns open/closed market-session status for symbols.
    Uses dynamic category detection when symbols.json is missing.

    Each entry:
      - symbol:    e.g. "EURUSD"
      - category:  e.g. "forex" (detected dynamically)
      - open:      True if market is currently open
      - reason:    Human-readable explanation when closed
    """
    from engine.session_filter import session_filter

    result = []
    seen: set[str] = set()

    # Try to get symbols from config first
    symbols_from_config = _all_symbols()
    
    if symbols_from_config:
        # Use configured symbols
        for sym, cat in symbols_from_config:
            if sym in seen:
                continue
            seen.add(sym)
            is_open, reason = session_filter.is_open(sym, cat)
            result.append({
                "symbol":   sym,
                "category": cat,
                "open":     is_open,
                "reason":   reason,
            })
    else:
        # No symbols.json - use a reasonable default set of symbols to check
        # These are the most commonly traded symbols
        default_symbols = [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
            "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY",
            "GOLD", "SILVER", "OILCash", "BRENTCash", "NGASCash",
            "US30Cash", "US100Cash", "US500Cash", "GER40Cash", "UK100Cash",
            "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD",
        ]
        for sym in default_symbols:
            if sym in seen:
                continue
            seen.add(sym)
            # Let session_filter detect category dynamically
            is_open, reason = session_filter.is_open(sym)
            cat = session_filter.category_for(sym)
            result.append({
                "symbol":   sym,
                "category": cat,
                "open":     is_open,
                "reason":   reason,
            })

    return {"sessions": result, "count": len(result)}


# ---------------------------------------------------------------------------
# POST /risk/reset-drawdown
# ---------------------------------------------------------------------------

@router.post("/reset-drawdown")
def reset_drawdown():
    """
    Manually clear the daily and weekly drawdown circuit-breaker halts.
    The drawdown start-balance anchors are reset to the current balance.
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    # Try to get current balance to anchor the new drawdown baseline
    current_balance: float | None = None
    try:
        from api.main import mt5_client
        if mt5_client is not None:
            info = mt5_client.get_account_info()
            if info:
                current_balance = info.get("balance")
    except Exception:
        pass

    rm.reset_drawdown(current_balance)
    return {"ok": True, "message": "Drawdown halts cleared", "new_balance_anchor": current_balance}


# ---------------------------------------------------------------------------
# POST /risk/reset-consecutive-losses
# ---------------------------------------------------------------------------

@router.post("/reset-consecutive-losses")
def reset_consecutive_losses():
    """Clear all consecutive-loss counters and mode pauses."""
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    rm.reset_consecutive_losses()
    return {"ok": True, "message": "Consecutive-loss counters and mode pauses cleared"}


# ---------------------------------------------------------------------------
# POST /risk/circuit-breaker
# ---------------------------------------------------------------------------

class CircuitBreakerToggle(dict):
    pass

@router.post("/circuit-breaker")
def set_circuit_breaker(body: dict):
    """
    Enable or disable the circuit breaker globally.

    Body: { "enabled": true | false }
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="'enabled' must be a boolean")

    rm.set_circuit_breaker_enabled(enabled)
    return {"ok": True, "circuit_breaker_enabled": enabled}


# ---------------------------------------------------------------------------
# GET /risk/strategy-status
# ---------------------------------------------------------------------------

@router.get("/strategy-status")
def strategy_circuit_breaker_status():
    """
    Returns per-strategy circuit-breaker state from the RiskManager.

    Response: dict keyed by strategy_name, each entry has:
      - consecutive_losses: int
      - paused_until: ISO timestamp or null
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    return rm.strategy_status()


# ---------------------------------------------------------------------------
# GET /risk/presets — Task #12
# ---------------------------------------------------------------------------

@router.get("/presets")
def get_risk_presets():
    """
    Returns all available risk presets and current selection.
    
    Response:
      - current:  Name of currently active preset
      - presets:  Dict of {preset_name: config} for all available presets
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    return rm.get_available_presets()


# ---------------------------------------------------------------------------
# POST /risk/presets/select — Task #12
# ---------------------------------------------------------------------------

@router.post("/presets/select")
def select_risk_preset(body: dict):
    """
    Set the active risk preset.
    
    Body:
      - preset: Name of preset to activate (conservative/moderate/aggressive)
    
    Returns:
      - ok:      True if successful
      - message: Success/error message
      - current: Name of now-active preset
    """
    from engine.risk_manager import RiskManager
    try:
        from api.main import get_risk_manager
        rm = get_risk_manager()
        if rm is None:
            raise AttributeError
    except (ImportError, AttributeError):
        rm = RiskManager()

    preset_name = body.get("preset")
    if not preset_name or not isinstance(preset_name, str):
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="'preset' field required (string)")

    success, message = rm.set_risk_preset(preset_name)
    
    if not success:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=message)
    
    return {
        "ok": True,
        "message": message,
        "current": preset_name
    }