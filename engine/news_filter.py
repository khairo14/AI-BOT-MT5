"""
News Filter — pauses trading around high-impact Forex Factory events.

Fetches the Forex Factory calendar via HTTP (JSON endpoint).
Caches results for 1 hour to avoid hammering the API.

Usage:
    from engine.news_filter import news_filter

    blocked, reason = news_filter.is_blocked(symbol="EURUSD", trading_type="scalping")
    if blocked:
        logger.info(f"Trading paused: {reason}")

News data: fetched from Forex Factory calendar API
Cache TTL: 1 hour (configurable via risk.json news_filter.cache_minutes)
Currencies detected from symbol automatically (EUR + USD from EURUSD).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"

# Forex Factory JSON calendar endpoint (public, no auth required)
_FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Map MT5 symbol prefixes → currency codes
_SYMBOL_CURRENCIES: dict[str, list[str]] = {
    "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"], "USDCHF": ["USD", "CHF"],
    "USDCAD": ["USD", "CAD"], "AUDUSD": ["AUD", "USD"],
    "NZDUSD": ["NZD", "USD"], "EURJPY": ["EUR", "JPY"],
    "GBPJPY": ["GBP", "JPY"], "EURGBP": ["EUR", "GBP"],
    "XAUUSD": ["XAU", "USD"], "GOLD"  : ["XAU", "USD"],
    "SILVER": ["XAG", "USD"], "BTCUSD": ["BTC", "USD"],
    "ETHUSD": ["ETH", "USD"],
}

UTC = ZoneInfo("UTC")


def _currencies_for(symbol: str) -> list[str]:
    """Return the currency codes affected by a symbol."""
    # Direct lookup first
    upper = symbol.upper().replace(".OQ", "").replace("CASH", "")
    if upper in _SYMBOL_CURRENCIES:
        return _SYMBOL_CURRENCIES[upper]
    # Indices / stocks / commodities → USD is the base
    return ["USD"]


class NewsFilter:
    """Polls Forex Factory, caches events, checks symbol impact windows."""

    def __init__(self):
        self._events:     list[dict] = []
        self._fetched_at: Optional[datetime] = None
        self._lock        = threading.Lock()
        self._cfg         = self._load_cfg()

    # ── public API ────────────────────────────────────────────────────────────

    def is_blocked(self, symbol: str, trading_type: str) -> tuple[bool, str]:
        """
        Returns (blocked, reason).
        Always returns (False, "") if news_filter.enabled = false in risk.json,
        or if this trading_type is not in affect_modes.
        """
        cfg = self._reload_cfg()
        if not cfg.get("enabled", True):
            return False, ""

        affect_modes = cfg.get("affect_modes", ["scalping", "day_trading"])
        if trading_type not in affect_modes:
            return False, ""

        self._refresh()
        currencies = _currencies_for(symbol)
        now        = datetime.now(tz=UTC)
        before_min = cfg.get("pause_minutes_before", 30)
        after_min  = cfg.get("pause_minutes_after", 15)
        impact_lvl = cfg.get("impact_filter", "high").lower()

        with self._lock:
            for ev in self._events:
                if not self._matches_impact(ev.get("impact", ""), impact_lvl):
                    continue
                if ev.get("currency", "").upper() not in currencies:
                    continue
                ev_time = self._parse_time(ev.get("date", ""), ev.get("time", ""))
                if ev_time is None:
                    continue
                window_start = ev_time - timedelta(minutes=before_min)
                window_end   = ev_time + timedelta(minutes=after_min)
                if window_start <= now <= window_end:
                    remaining = int((window_end - now).total_seconds() / 60)
                    return True, (
                        f"News blackout: {ev.get('title','?')} "
                        f"({ev.get('currency','?')}) in {remaining}min"
                    )
        return False, ""

    def next_events(self, currencies: Optional[list[str]] = None, n: int = 5) -> list[dict]:
        """Return the next N upcoming high-impact events, optionally filtered by currency."""
        self._refresh()
        now    = datetime.now(tz=UTC)
        impact = self._reload_cfg().get("impact_filter", "high").lower()
        result = []
        with self._lock:
            for ev in sorted(self._events, key=lambda e: e.get("_dt", datetime.max)):
                ev_time = ev.get("_dt")
                if ev_time is None or ev_time < now:
                    continue
                if not self._matches_impact(ev.get("impact", ""), impact):
                    continue
                if currencies and ev.get("currency", "").upper() not in currencies:
                    continue
                result.append({
                    "title":    ev.get("title", ""),
                    "currency": ev.get("currency", ""),
                    "impact":   ev.get("impact", ""),
                    "time_utc": ev_time.isoformat(),
                })
                if len(result) >= n:
                    break
        return result

    def status(self) -> dict:
        return {
            "enabled":      self._reload_cfg().get("enabled", True),
            "cached_events": len(self._events),
            "last_fetch":   self._fetched_at.isoformat() if self._fetched_at else None,
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Re-fetch from Forex Factory if cache is stale."""
        cfg       = self._reload_cfg()
        cache_min = cfg.get("cache_minutes", 60)
        now       = datetime.now(tz=UTC)
        with self._lock:
            if (
                self._fetched_at is not None
                and (now - self._fetched_at).total_seconds() < cache_min * 60
            ):
                return   # cache still fresh

        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            import httpx
            resp = httpx.get(_FF_URL, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
            events = []
            for ev in raw:
                dt = self._parse_time(ev.get("date", ""), ev.get("time", ""))
                ev["_dt"] = dt
                events.append(ev)
            with self._lock:
                self._events     = events
                self._fetched_at = datetime.now(tz=UTC)
            logger.info(f"NewsFilter: fetched {len(events)} events from Forex Factory")
        except Exception as exc:
            logger.warning(f"NewsFilter fetch failed: {exc} — using cached data")

    @staticmethod
    def _parse_time(date_str: str, time_str: str) -> Optional[datetime]:
        """Parse Forex Factory date+time strings into UTC datetime."""
        try:
            # FF format examples: date="03-17-2026", time="8:30am"
            dt_str = f"{date_str} {time_str}"
            dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
            # FF times are US/Eastern — convert to UTC (+5h standard, +4h DST)
            # Use a fixed offset approximation (EST = UTC-5)
            return dt.replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)
        except Exception:
            return None

    @staticmethod
    def _matches_impact(impact: str, filter_level: str) -> bool:
        """Return True if event impact meets or exceeds the configured filter."""
        order = {"low": 0, "medium": 1, "high": 2}
        ev_lvl  = order.get(impact.lower(), 0)
        req_lvl = order.get(filter_level, 2)
        return ev_lvl >= req_lvl

    def _load_cfg(self) -> dict:
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f).get("news_filter", {})
        except Exception:
            return {}

    def _reload_cfg(self) -> dict:
        """Re-read config each call so live dashboard changes take effect."""
        self._cfg = self._load_cfg()
        return self._cfg


# Application-level singleton
news_filter = NewsFilter()
