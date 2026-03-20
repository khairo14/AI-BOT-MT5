"""
Session Filter — enforces market-hours trading rules per instrument category.

Rules are read from config/risk.json under session_filter.sessions.
Each category has an open/close time in UTC (or server time) and valid days.

Usage:
    from engine.session_filter import session_filter

    allowed, reason = session_filter.is_open(symbol="TSLA.OQ", category="stock")
    if not allowed:
        logger.info(f"Market closed: {reason}")

All times in risk.json are interpreted as UTC.
Crypto is always open (00:00–23:59, all days).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"
UTC = timezone.utc

# Symbol → session category mapping
_SYMBOL_CATEGORY: dict[str, str] = {}   # built lazily from symbols.json

_SYMBOLS_PATH = Path(__file__).parent.parent / "config" / "symbols.json"


def _load_symbol_categories() -> dict[str, str]:
    """Build a flat symbol→category map from symbols.json."""
    if not _SYMBOLS_PATH.exists():
        return {}
    try:
        with open(_SYMBOLS_PATH) as f:
            data = json.load(f)
        result: dict[str, str] = {}
        for _mode, symbols in data.items():
            for s in symbols:
                result[s["symbol"].upper()] = s.get("category", "forex")
        return result
    except Exception:
        return {}


class SessionFilter:
    """
    Checks whether a symbol's market is currently open.
    Reads session schedule from risk.json and symbol categories from symbols.json.
    """

    def __init__(self):
        self._sym_cat = _load_symbol_categories()
        self._sym_cat_lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────────

    def is_open(self, symbol: str, category: Optional[str] = None) -> tuple[bool, str]:
        """
        Returns (is_open, reason).
        Always returns (True, "") if session_filter.enabled = false.
        """
        cfg = self._load_cfg()
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
        if not self._day_allowed(now, days):
            return False, f"{symbol} market closed — weekend"

        open_t  = self._parse_time(session.get("open",  "00:00"))
        close_t = self._parse_time(session.get("close", "23:59"))
        current = now.time().replace(second=0, microsecond=0)

        if open_t <= close_t:
            in_session = open_t <= current <= close_t
        else:
            # Overnight session (wraps midnight)
            in_session = current >= open_t or current <= close_t

        if not in_session:
            return False, (
                f"{symbol} market closed — session {session.get('open')}–{session.get('close')} UTC"
            )
        return True, ""

    def category_for(self, symbol: str) -> str:
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

    # ── internal ──────────────────────────────────────────────────────────────

    def _get_category(self, symbol: str) -> str:
        with self._sym_cat_lock:
            if not self._sym_cat:
                self._sym_cat = _load_symbol_categories()
        return self._sym_cat.get(symbol.upper(), "forex")

    @staticmethod
    def _category_to_session(cat: str) -> str:
        return {
            "stock":    "us_stocks",
            "us_index": "us_indices",
            "eu_index": "eu_indices",
            "index":    "eu_indices",   # legacy fallback
            "commodity": "commodities",
            "forex":    "forex",
            "crypto":   "crypto",
        }.get(cat, "forex")

    @staticmethod
    def _parse_time(t_str: str) -> time:
        try:
            h, m = t_str.split(":")
            return time(int(h), int(m))
        except Exception:
            return time(0, 0)

    @staticmethod
    def _day_allowed(dt: datetime, days_spec: str) -> bool:
        if days_spec == "all":
            return True
        wd = dt.weekday()   # 0=Mon … 6=Sun
        if days_spec == "mon-fri":
            return wd <= 4   # Mon–Fri
        return True

    @staticmethod
    def _load_cfg() -> dict:
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f).get("session_filter", {})
        except Exception:
            return {}


# Application-level singleton
session_filter = SessionFilter()
