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


def _all_symbols() -> list[tuple[str, str]]:
    """Return [(symbol, category), ...] for every symbol in symbols.json."""
    if not _SYMBOLS_PATH.exists():
        return []
    try:
        with open(_SYMBOLS_PATH) as f:
            data = json.load(f)
        result: list[tuple[str, str]] = []
        for _mode, symbols in data.items():
            for s in symbols:
                result.append((s["symbol"], s.get("category", "forex")))
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
        from api.runner_loop import _risk_manager as rm  # type: ignore[attr-defined]
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
    Returns open/closed market-session status for every configured symbol.

    Each entry:
      - symbol:    e.g. "EURUSD"
      - category:  e.g. "forex"
      - open:      True if market is currently open
      - reason:    Human-readable explanation when closed
    """
    from engine.session_filter import session_filter

    result = []
    seen: set[str] = set()
    for sym, cat in _all_symbols():
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

    return {"sessions": result, "count": len(result)}
