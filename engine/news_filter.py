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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"

# Forex Factory JSON calendar endpoint (public, no auth required).
# Only the thisweek endpoint is guaranteed to exist on the CDN.
# For next-week coverage we build a date-based URL using the ISO start of
# next Monday and try an alternate FF CDN pattern; if that also 404s we
# silently fall back to thisweek-only (still covers Mon–Sun of current week).
_FF_URL_THIS = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Map MT5 symbol prefixes → currency codes
_SYMBOL_CURRENCIES: dict[str, list[str]] = {
    # Forex majors
    "EURUSD": ["EUR", "USD"], "GBPUSD": ["GBP", "USD"],
    "USDJPY": ["USD", "JPY"], "USDCHF": ["USD", "CHF"],
    "USDCAD": ["USD", "CAD"], "AUDUSD": ["AUD", "USD"],
    "NZDUSD": ["NZD", "USD"], "EURJPY": ["EUR", "JPY"],
    "GBPJPY": ["GBP", "JPY"], "EURGBP": ["EUR", "GBP"],
    # Metals / commodities (USD-denominated)
    "XAUUSD": ["XAU", "USD"], "GOLD"  : ["XAU", "USD"],
    "SILVER": ["XAG", "USD"],
    "OILCASH": ["USD"],   "BRENTCASH": ["USD"],   "NGASCASH": ["USD"],
    # Crypto (USD-denominated)
    "BTCUSD": ["BTC", "USD"], "ETHUSD": ["ETH", "USD"],
    "SOLUSD": ["USD"],        "XRPUSD": ["USD"],
    # US equities (USD macro news matters)
    "APPLE": ["USD"],   "AMAZON": ["USD"],    "TESLA": ["USD"],
    "MICROSOFT": ["USD"], "GOOGLE": ["USD"],  "FACEBOOK": ["USD"],
    "NETFLIX": ["USD"], "NVIDIA": ["USD"],    "ADVMICRODEV": ["USD"],
    # US indices (USD macro-driven)
    "US30CASH": ["USD"], "US100CASH": ["USD"], "US500CASH": ["USD"],
    # EU / UK indices
    "GER40CASH": ["EUR"], "UK100CASH": ["GBP"],
}

# Stock symbols → ticker mapping for earnings calendar lookup
# Used to fetch earnings dates from open APIs
_STOCK_TICKERS: dict[str, str] = {
    "TESLA":      "TSLA",
    "NVIDIA":     "NVDA",
    "APPLE":      "AAPL",
    "MICROSOFT":  "MSFT",
    "AMAZON":     "AMZN",
    "GOOGLE":     "GOOGL",
    "FACEBOOK":   "META",
    "NETFLIX":    "NFLX",
    "ADVMICRODEV": "AMD",
}

# Earnings blackout window — pause trading around earnings releases
# Stocks can move 10-20% on earnings; wider window than normal news
_EARNINGS_PAUSE_BEFORE_HOURS = 2   # 2 hours before earnings
_EARNINGS_PAUSE_AFTER_HOURS  = 4   # 4 hours after earnings

UTC = timezone.utc


def _currencies_for(symbol: str) -> list[str]:
    """Return the currency codes affected by a symbol.

    Handles broker-specific suffixes such as:
      - Trailing digits or letters  (e.g. EURUSD.r, EURUSD2, GBPUSD.p)
      - .OQ / .N / .a / .b / .Z    (Reuters/broker variants)
      - Cash suffix                 (US30Cash → US30CASH in map)
      - MT5 hash suffixes           (XAUUSD#, BTCUSD+)

    Resolution order:
      1. Exact match in _SYMBOL_CURRENCIES (fastest, most common)
      2. Strip common broker suffixes and retry
      3. Prefix match — any key that the normalised symbol starts with
      4. Fallback: USD (covers unlisted instruments that are USD-denominated)
    """
    # Step 1: exact match with light normalisation
    raw_upper = symbol.upper()
    for candidate in (
        raw_upper,
        raw_upper.replace(".OQ", "").replace("CASH", "").rstrip(".,;#+* "),
        raw_upper.replace(".OQ", ""),
    ):
        if candidate in _SYMBOL_CURRENCIES:
            return _SYMBOL_CURRENCIES[candidate]

    # Step 2: strip broker suffix patterns and retry
    import re as _re
    # Remove trailing non-alpha-digit characters, then trailing digits/letters added by broker
    stripped = _re.sub(r"[#\+\.\-\*;,]+.*$", "", raw_upper)   # cut at first special char
    stripped = _re.sub(r"[A-Z]?\d+$", "", stripped)            # strip trailing digit(s) + optional letter
    if stripped and stripped in _SYMBOL_CURRENCIES:
        return _SYMBOL_CURRENCIES[stripped]

    # Step 3: prefix match (e.g. "EURUSDPRO" → look for "EURUSD")
    for key in _SYMBOL_CURRENCIES:
        if stripped.startswith(key) or raw_upper.startswith(key):
            return _SYMBOL_CURRENCIES[key]

    # Step 4: fallback — most unlisted instruments are USD-denominated
    return ["USD"]


class NewsFilter:
    """Polls Forex Factory, caches events, checks symbol impact windows."""

    def __init__(self):
        self._events:              list[dict]         = []
        self._fetched_at:          Optional[datetime] = None
        self._lock                 = threading.Lock()
        self._refresh_in_progress: bool               = False
        self._cfg_cache:           Optional[dict]     = None
        self._cfg_loaded_at:       Optional[datetime] = None
        self._cfg = self._load_cfg()
        # Earnings calendar cache — keyed by ticker → list of earnings datetimes
        self._earnings_cache:      dict[str, list[datetime]] = {}
        self._earnings_fetched_at: Optional[datetime]        = None
        self._earnings_refresh_in_progress: bool             = False

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
    
        # Earnings blackout for stock symbols
        _ticker = _STOCK_TICKERS.get(symbol.upper())
        if _ticker and trading_type in affect_modes:
            blocked, reason = self._check_earnings(symbol, _ticker)
            if blocked:
                return True, reason

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
        with self._lock:
            earnings_count = sum(len(v) for v in self._earnings_cache.values())
        return {
            "enabled":        self._reload_cfg().get("enabled", True),
            "cached_events":  len(self._events),
            "last_fetch":     self._fetched_at.isoformat() if self._fetched_at else None,
            "earnings_cached": earnings_count,
            "earnings_tickers": list(self._earnings_cache.keys()),
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        """Re-fetch from Forex Factory if cache is stale. Thread-safe, no duplicate fetches."""
        cfg       = self._reload_cfg()
        cache_min = cfg.get("cache_minutes", 60)
        now       = datetime.now(tz=UTC)
        with self._lock:
            if (
                self._fetched_at is not None
                and (now - self._fetched_at).total_seconds() < cache_min * 60
            ):
                return   # cache still fresh
            if self._refresh_in_progress:
                return   # another thread is already fetching
            self._refresh_in_progress = True
            threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        try:
            import httpx
            from datetime import timedelta as _td

            events: list[dict] = []

            # ── Step 1: always fetch this week (guaranteed to exist) ──────────
            try:
                resp = httpx.get(_FF_URL_THIS, timeout=10)
                resp.raise_for_status()
                for ev in resp.json():
                    dt = self._parse_time(ev.get("date", ""), ev.get("time", ""))
                    ev["_dt"] = dt
                    events.append(ev)
                logger.debug(f"NewsFilter: thisweek — {len(events)} events")
            except Exception as _this_exc:
                logger.warning(f"NewsFilter: thisweek fetch failed: {_this_exc}")

            # ── Step 2: attempt next-week using a date-based query param ─────
            # FF CDN supports ?week=YYYY-MM-DD where the date is any day in the
            # target ISO week. We pass next Monday's date.
            # If this endpoint also returns 404 or errors, we silently skip —
            # thisweek already covers Mon-Sun of the current week and most
            # high-impact events are announced ≥ 2 business days in advance.
            try:
                _now_utc   = datetime.now(tz=UTC)
                _days_to_monday = (7 - _now_utc.weekday()) % 7 or 7
                _next_monday = (_now_utc + _td(days=_days_to_monday)).strftime("%Y-%m-%d")
                _next_url  = f"{_FF_URL_THIS}?week={_next_monday}"
                _resp2 = httpx.get(_next_url, timeout=10)
                if _resp2.status_code == 200:
                    _seen = {(ev.get("date"), ev.get("time"), ev.get("title"))
                             for ev in events}
                    _added = 0
                    for ev in _resp2.json():
                        _key = (ev.get("date"), ev.get("time"), ev.get("title"))
                        if _key not in _seen:
                            dt = self._parse_time(ev.get("date", ""), ev.get("time", ""))
                            ev["_dt"] = dt
                            events.append(ev)
                            _seen.add(_key)
                            _added += 1
                    if _added:
                        logger.debug(f"NewsFilter: nextweek — added {_added} events")
                # 404 or other non-200 → silently ignore (endpoint may not exist)
            except Exception:
                pass   # nextweek fetch failure is non-critical

            with self._lock:
                self._events     = events
                self._fetched_at = datetime.now(tz=UTC)
            logger.info(f"NewsFilter: loaded {len(events)} calendar events")
        except Exception as exc:
            logger.warning(f"NewsFilter fetch failed: {exc} — using cached data")
        finally:
            with self._lock:
                self._refresh_in_progress = False

    def _check_earnings(self, symbol: str, ticker: str) -> tuple[bool, str]:
        """Check if trading should be paused due to upcoming/recent earnings."""
        self._refresh_earnings(ticker)
        now = datetime.now(tz=UTC)
        with self._lock:
            earnings_dates = self._earnings_cache.get(ticker, [])
        for dt in earnings_dates:
            window_start = dt - timedelta(hours=_EARNINGS_PAUSE_BEFORE_HOURS)
            window_end   = dt + timedelta(hours=_EARNINGS_PAUSE_AFTER_HOURS)
            if window_start <= now <= window_end:
                if now < dt:
                    mins = int((dt - now).total_seconds() / 60)
                    return True, f"Earnings blackout: {symbol} reports in {mins}min"
                else:
                    mins = int((now - dt).total_seconds() / 60)
                    return True, f"Earnings blackout: {symbol} reported {mins}min ago"
        return False, ""

    def _refresh_earnings(self, ticker: str) -> None:
        """Fetch earnings dates from open API — cached for 6 hours."""
        now = datetime.now(tz=UTC)
        with self._lock:
            if (
                self._earnings_fetched_at is not None
                and (now - self._earnings_fetched_at).total_seconds() < 6 * 3600
                and ticker in self._earnings_cache
            ):
                return
            if self._earnings_refresh_in_progress:
                return
            self._earnings_refresh_in_progress = True
        threading.Thread(
            target=self._fetch_earnings,
            args=(ticker,),
            daemon=True,
        ).start()

    def _fetch_earnings(self, ticker: str) -> None:
        """
        Fetch upcoming earnings dates from Nasdaq earnings calendar API.
        Uses public endpoint — no API key required.
        Falls back gracefully if fetch fails.
        """
        try:
            import httpx
            # Nasdaq public earnings calendar — returns JSON with earnings dates
            url = f"https://api.nasdaq.com/api/calendar/earnings?date={datetime.now(tz=UTC).strftime('%Y-%m-%d')}"
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = httpx.get(url, timeout=10, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            rows = data.get("data", {}).get("rows", []) or []
            dates: list[datetime] = []
            for row in rows:
                if row.get("symbol", "").upper() != ticker.upper():
                    continue
                # Time format: "After Market Close" / "Before Market Open" / "Time Not Supplied"
                time_str = row.get("time", "")
                date_str = row.get("lastReportedDate") or datetime.now(tz=UTC).strftime("%m/%d/%Y")
                try:
                    from datetime import date as _date
                    _d = datetime.strptime(date_str, "%m/%d/%Y")
                    # Map time description to approximate UTC time
                    if "After" in time_str:
                        # After market close ~21:00 ET = ~01:00 UTC next day
                        _dt = _d.replace(hour=21, minute=0)
                    elif "Before" in time_str:
                        # Before market open ~12:30 ET = ~16:30 UTC
                        _dt = _d.replace(hour=12, minute=30)
                    else:
                        # Unknown time — use market close as conservative estimate
                        _dt = _d.replace(hour=21, minute=0)
                    # Convert ET to UTC (approximate — DST not critical for earnings window)
                    _dt_utc = _dt.replace(tzinfo=timezone(timedelta(hours=-4))).astimezone(UTC)
                    dates.append(_dt_utc)
                except Exception:
                    continue
            with self._lock:
                self._earnings_cache[ticker] = dates
                self._earnings_fetched_at = datetime.now(tz=UTC)
            if dates:
                logger.info(f"NewsFilter: fetched {len(dates)} earnings date(s) for {ticker}")
        except Exception as exc:
            logger.debug(f"NewsFilter: earnings fetch failed for {ticker}: {exc}")
            with self._lock:
                # Cache empty list so we don't retry immediately on failure
                if ticker not in self._earnings_cache:
                    self._earnings_cache[ticker] = []
                self._earnings_fetched_at = datetime.now(tz=UTC)
        finally:
            with self._lock:
                self._earnings_refresh_in_progress = False

    @staticmethod
    def _parse_time(date_str: str, time_str: str) -> Optional[datetime]:
        """Parse Forex Factory date+time strings into UTC datetime.

        LOGIC-5 / RISK-4: ForexFactory publishes all event times in US/Eastern
        (America/New_York), which observes Daylight Saving Time (EDT = UTC-4
        from 2nd Sunday in March through 1st Sunday in November; EST = UTC-5
        the rest of the year).  This is documented in the FF FAQ and confirmed
        by cross-checking FF event times with Reuters/Bloomberg timestamps.

        The conversion below uses ``ZoneInfo("America/New_York")`` with DST-aware
        arithmetic when the tzdata package is available (Python 3.9+), or falls
        back to manual US DST rule calculation otherwise.  Hardcoding UTC-5 or
        UTC-4 year-round would shift every spring/autumn event time by ±1 hour.
        """
        try:
            # FF format examples: date="03-17-2026", time="8:30am"
            dt_str = f"{date_str} {time_str}"
            dt = datetime.strptime(dt_str, "%m-%d-%Y %I:%M%p")
            # FF times are US/Eastern — convert to UTC
            try:
                tz: datetime.tzinfo = ZoneInfo("America/New_York")
            except (KeyError, ZoneInfoNotFoundError):
                # tzdata package not installed (common on Windows without tzdata).
                # Compute the correct US offset for this specific date (EST vs EDT)
                # rather than hardcoding UTC-5 year-round (wrong March-November).
                # US DST: 2nd Sunday in March → 1st Sunday in November.
                from datetime import timedelta as _td
                _year = dt.year
                # 2nd Sunday in March
                _mar1 = datetime(_year, 3, 1)
                _dst_start = _mar1 + _td(days=(6 - _mar1.weekday()) % 7 + 7)
                # 1st Sunday in November
                _nov1 = datetime(_year, 11, 1)
                _dst_end = _nov1 + _td(days=(6 - _nov1.weekday()) % 7)
                _is_edt = _dst_start <= dt < _dst_end
                tz = timezone(_td(hours=-4 if _is_edt else -5))
            return dt.replace(tzinfo=tz).astimezone(UTC)
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
        """Return config, re-reading from disk at most every 60 seconds."""
        now = datetime.now(tz=UTC)
        if (
            self._cfg_cache is not None
            and self._cfg_loaded_at is not None
            and (now - self._cfg_loaded_at).total_seconds() < 60
        ):
            return self._cfg_cache
        self._cfg_cache = self._load_cfg()
        self._cfg_loaded_at = now
        return self._cfg_cache


# Application-level singleton
news_filter = NewsFilter()
