"""
Session Filter — enforces market-hours trading rules per instrument category.

Rules are read from config/risk.json under session_filter.sessions.
Each category has an open/close time in UTC (or server time) and valid days.

Usage:
    from engine.session_filter import session_filter

    allowed, reason = session_filter.is_open(symbol="TSLA.OQ")
    if not allowed:
        logger.info(f"Market closed: {reason}")

All times in risk.json are interpreted as UTC.
Crypto is always open (00:00–23:59, all days).
"""

from __future__ import annotations

import json
import threading
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"
UTC = timezone.utc

# TTL cache for risk.json — read at most once every 30 seconds
_cfg_cache: dict = {}
_cfg_loaded_at: float = 0.0
_CFG_TTL = 30.0
_cfg_lock = threading.Lock()


def _load_cfg() -> dict:
    """Load risk.json session_filter config with TTL cache."""
    global _cfg_cache, _cfg_loaded_at
    now = _time.monotonic()
    if now - _cfg_loaded_at < _CFG_TTL:
        return _cfg_cache
    with _cfg_lock:
        if now - _cfg_loaded_at < _CFG_TTL:
            return _cfg_cache
        try:
            with open(CONFIG_PATH) as f:
                _cfg_cache = json.load(f).get("session_filter", {})
        except Exception:
            _cfg_cache = {}
        _cfg_loaded_at = now
    return _cfg_cache


def _detect_category_from_symbol(symbol: str) -> str:
    """
    Dynamically detect asset category from symbol name.
    This is the sole method of category detection - no config files needed.
    """
    # Strip common suffixes (#, ., _, -) for detection
    clean = symbol.upper().rstrip("#+*!._-")
    
    # Crypto - trade 24/7
    crypto = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "DOT", "LTC", "BNB", "XLM", "ETC", "GRT"}
    if any(c in clean for c in crypto):
        return "crypto"
    
    # US Stocks (company names) - follow NYSE/NASDAQ hours
    stocks = {"TSLA", "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "NFLX", "AMD", "INTC"}
    if any(stk in clean for stk in stocks):
        return "stock"
    
    # US Indices - follow CME/NYSE hours
    us_indices = {"US30", "US100", "US500", "SPX", "NAS", "NDX"}
    if any(idx in clean for idx in us_indices):
        return "us_index"
    
    # EU Indices - follow EU market hours
    eu_indices = {"GER40", "DAX", "UK100", "FRA40", "EU50"}
    if any(idx in clean for idx in eu_indices):
        return "eu_index"
    
    # Commodities (Gold, Silver, Oil, Gas) - follow COMEX/NYMEX hours
    commodities = {"GOLD", "XAU", "SILVER", "XAG", "OIL", "BRENT", "WTI", "NGAS"}
    if any(cmd in clean for cmd in commodities):
        return "commodity"
    
    # Forex - check if the stripped symbol is a 6-letter forex pair OR contains currency codes
    # Remove any remaining non-letter characters after stripping common suffixes
    forex_chars = ''.join(c for c in clean if c.isalpha())
    
    # Standard forex: 6 letters (e.g., EURUSD, GBPJPY)
    if len(forex_chars) == 6:
        # Check if it's a valid currency pair structure (3 letters + 3 letters)
        first_three = forex_chars[:3]
        last_three = forex_chars[3:]
        # Basic currency codes
        currencies = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SGD", "HKD", "NOK", "SEK"}
        if first_three in currencies and last_three in currencies:
            return "forex"
    
    # Also detect if symbol contains common currency pairs (e.g., contains "EUR" and "USD")
    # This catches symbols like "EURUSD#", "GBPUSD.", "EURGBP_i"
    if ("EUR" in clean and "USD" in clean) or \
       ("GBP" in clean and "USD" in clean) or \
       ("USD" in clean and "JPY" in clean) or \
       ("AUD" in clean and "USD" in clean) or \
       ("USD" in clean and "CAD" in clean) or \
       ("NZD" in clean and "USD" in clean):
        return "forex"
    
    # Default to forex for anything else
    return "forex"


# ── US market holiday helpers ──────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """Return the nth occurrence (1-based) of *weekday* (0=Mon, 3=Thu, etc.) in year/month."""
    first = date(year, month, 1)
    first_occurrence = first + timedelta(days=(weekday - first.weekday()) % 7)
    return first_occurrence + timedelta(weeks=n - 1)


def _last_weekday_in_month(year: int, month: int, weekday: int) -> date:
    """Return the last occurrence of *weekday* in year/month."""
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Compute Easter Sunday using the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day   = (h + ll - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(d: date) -> date:
    """If holiday falls on Saturday → Friday observed; Sunday → Monday observed."""
    if d.weekday() == 5:   # Saturday
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=10)
def _us_market_holidays(year: int) -> frozenset:
    """
    Return the set of NYSE/NASDAQ market-closed dates for *year*.
    Covers: New Year's Day, MLK Day, Presidents' Day, Good Friday,
    Memorial Day, Juneteenth, Independence Day, Labor Day,
    Thanksgiving, Christmas.
    Results are cached per year via lru_cache.
    """
    holidays: set[date] = set()

    # Fixed calendar dates (with observed shift for weekends)
    for month, day in [(1, 1), (6, 19), (7, 4), (12, 25)]:
        holidays.add(_observed(date(year, month, day)))

    # Floating / rule-based holidays
    holidays.add(_nth_weekday(year, 1, 0, 3))               # MLK Day:        3rd Mon Jan
    holidays.add(_nth_weekday(year, 2, 0, 3))               # Presidents Day: 3rd Mon Feb
    holidays.add(_easter(year) - timedelta(days=2))         # Good Friday:    2 days before Easter
    holidays.add(_last_weekday_in_month(year, 5, 0))        # Memorial Day:   last Mon May
    holidays.add(_nth_weekday(year, 9, 0, 1))               # Labor Day:      1st Mon Sep
    holidays.add(_nth_weekday(year, 11, 3, 4))              # Thanksgiving:   4th Thu Nov

    return frozenset(holidays)


class SessionFilter:
    """
    Checks whether a symbol's market is currently open.
    Reads session schedule from risk.json and detects categories from symbol name.
    No external config files required.
    """

    def __init__(self):
        # Category cache to avoid repeated detection
        self._category_cache: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def is_open(self, symbol: str, category: Optional[str] = None) -> tuple[bool, str]:
        """
        Returns (is_open, reason).
        Always returns (True, "") if session_filter.enabled = false.
        """
        cfg = _load_cfg()  # Use cached config
        if not cfg.get("enabled", True):
            return True, ""

        cat = (category or self._get_category(symbol)).lower()
        sessions = cfg.get("sessions", {})

        # Crypto: always open
        if cat == "crypto":
            return True, ""

        # Map category to session key
        session_key = self._category_to_session(cat)
        session     = sessions.get(session_key)
        if session is None:
            # No rule defined → allow
            return True, ""

        now  = datetime.now(tz=UTC)
        days = session.get("days", "mon-fri")
        if not self._day_allowed(now, days, cat):
            return False, f"{symbol} market closed — weekend or holiday"

        open_t  = self._parse_time(session.get("open",  "00:00"))
        close_t = self._parse_time(session.get("close", "23:59"))
        current = now.time().replace(second=0, microsecond=0)

        if open_t <= close_t:
            # use exclusive close boundary — at exactly close_t the session is considered closed
            in_session = open_t <= current < close_t
        else:
            # Overnight session (wraps midnight) — exclusive on the morning close side
            in_session = current >= open_t or current < close_t

        if not in_session:
            return False, (
                f"{symbol} market closed — session {session.get('open')}–{session.get('close')} UTC"
            )
        return True, ""

    def category_for(self, symbol: str) -> str:
        """Return the detected category for a symbol."""
        return self._get_category(symbol)

    def status(self, symbols: list[str]) -> dict:
        """Return open/closed status for a list of symbols."""
        return {
            sym: {
                "open":     self.is_open(sym)[0],
                "category": self._get_category(sym),
            }
            for sym in symbols
        }

    def clear_cache(self) -> None:
        """Clear the category detection cache."""
        with self._cache_lock:
            self._category_cache.clear()

    # ── internal ──────────────────────────────────────────────────────────────

    def _get_category(self, symbol: str) -> str:
        """Return category for symbol, using cache and dynamic detection."""
        # Normalize: strip suffixes for consistent caching
        clean = symbol.upper().rstrip("#+*!._-")
        
        with self._cache_lock:
            if clean in self._category_cache:
                return self._category_cache[clean]
            
            # Pass the cleaned symbol to detection function
            cat = _detect_category_from_symbol(clean)
            self._category_cache[clean] = cat
            return cat

    @staticmethod
    def _category_to_session(cat: str) -> str:
        return {
            "stock":     "us_stocks",
            "us_index":  "us_indices",
            "eu_index":  "eu_indices",
            "index":     "eu_indices",   # legacy fallback
            "commodity": "commodities",
            "forex":     "forex",
            "crypto":    "crypto",
        }.get(cat, "forex")

    @staticmethod
    def _parse_time(t_str: str) -> time:
        try:
            h, m = t_str.split(":")
            return time(int(h), int(m))
        except Exception:
            return time(0, 0)

    @staticmethod
    def _day_allowed(dt: datetime, days_spec: str, category: str = "forex") -> bool:
        if days_spec == "all":
            return True
        wd = dt.weekday()   # 0=Mon … 6=Sun
        if wd > 4:
            return False   # weekend
        
        if days_spec == "mon-fri":
            # For US equity and index categories, also block on US market holidays
            if category in ("stock", "us_index"):
                return dt.date() not in _us_market_holidays(dt.year)
            # EU indices have weekend closures but no holiday calendar
            if category == "eu_index":
                return True  # Only weekends matter for EU
            return True
        return True


# Application-level singleton
session_filter = SessionFilter()