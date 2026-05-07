"""
Trade Journal — append-only JSONL store for all bot-executed trades.

Every trade placed by the bot (auto or manual approval) is appended here
with its account_mode tag ("demo" / "live") and account_login, so the
dashboard can show a unified, filterable history across all accounts.

Storage: data/trade_journal.jsonl (one JSON object per line)

Usage:
    from engine.trade_journal import trade_journal

    trade_journal.log(ticket=12345, symbol="EURUSD", direction="buy",
                      volume=0.1, entry=1.0850, sl=1.0820, tp=1.0910,
                      profit=None, trading_type="scalping",
                      account_mode="demo", account_login=1301109267,
                      comment="EMAScalp")

    entries = trade_journal.get(account="demo", account_login=1301109267, limit=50)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional


from loguru import logger
from engine.utils.symbol_utils import normalize_symbol

_JOURNAL_PATH = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"

def normalize_account_mode(value: str) -> str:
    value = str(value or "live").lower().strip()
    return "demo" if value == "paper" else value

class TradeJournal:
    """Thread-safe append-only JSONL trade journal."""

    def __init__(self, path: Path = _JOURNAL_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────

    def log(
        self,
        *,
        ticket: int,
        symbol: str,
        direction: str,
        volume: float,
        entry: float,
        sl: float,
        tp: Optional[float],
        profit: Optional[float],
        trading_type: str,
        account_mode: str,
        comment: str = "",
        open_time: Optional[str] = None,
        close_time: Optional[str] = None,
        event: Literal["open", "close", "partial_close"] = "open",
        confidence: Optional[float] = None,
        expected_price: Optional[float] = None,
        slippage: Optional[float] = None,
        execution_time_ms: Optional[int] = None,
        spread_pips: Optional[float] = None,
        swap: Optional[float] = None,
        commission: Optional[float] = None,
        tp2: Optional[float] = None,
        tp3: Optional[float] = None,
        account_login: int = 0,
        account_type: str = "",
        user_id: str = "default",
        strategy: str = "",
    ) -> None:
        """Append a trade event to the journal."""
        record = {
            "ticket":       ticket,
            # Symbol identity
            "symbol":       symbol,
            "symbol_raw":   symbol,
            "symbol_normalized": normalize_symbol(symbol),

            "direction":    direction.upper(),
            "volume":       volume,
            "entry":        entry,
            "sl":           sl,
            "tp":           tp,
            "tp2":          tp2,
            "tp3":          tp3,
            "profit":       profit,
            # Strategy/mode identity
            "trading_type": trading_type,
            "strategy":     strategy or comment,
             # Account identity
            "account_mode": normalize_account_mode(account_mode),
            "account_login": account_login,
            "account_type": account_type,
            "user_id":      user_id,

            "comment":      comment,
            "event":        event,
            "open_time":    (open_time or datetime.now(tz=timezone.utc).isoformat()) if event == "open" else open_time,
            "close_time":   close_time,
            "logged_at":    datetime.now(tz=timezone.utc).isoformat(),
            "confidence":   confidence,
            "expected_price":    expected_price,
            "slippage":          slippage,
            "execution_time_ms": execution_time_ms,
            "spread_pips":       spread_pips,
            "swap":              swap,
            "commission":        commission,
        }
        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
            except Exception as exc:
                logger.error(f"TradeJournal write error: {exc}")

    def get(
        self,
        account: str = "all",
        trading_type: Optional[str] = None,
        event: Optional[str] = None,
        limit: int = 100,
        account_login: Optional[int] = None,
    ) -> list[dict]:
        """
        Read journal entries, newest first.

        Args:
            account:       "demo", "live", or "all"
            trading_type:  "scalping", "day_trading", "swing", or None
            event:         "open", "close", or None
            limit:         max entries to return
            account_login: filter by specific MT5 account number
        """
        account = normalize_account_mode(account)
        if not self._path.exists():
            return []

        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except Exception:
                return []

        results: list[dict] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if account != "all" and record.get("account_mode") != account:
                continue
            if account_login is not None and record.get("account_login") != account_login:
                continue
            if trading_type and record.get("trading_type") != trading_type:
                continue
            if event and record.get("event") != event:
                continue
            results.append(record)
            if len(results) >= limit:
                break

        return results

    def stats(self, account: str = "all", account_login: Optional[int] = None) -> dict:
        """Return summary stats for the given account filter."""
        entries = self.get(account=account, limit=10_000, account_login=account_login)
        closed = [
            e for e in entries
            if e.get("event") in ("close", "partial_close") and e.get("profit") is not None
        ]
        if not closed:
            return {
                "total": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_profit": 0.0,
                "today_trades": 0, "today_pnl": 0.0,
            }

        today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        today = [
            e for e in closed
            if (e.get("close_time") or e.get("logged_at") or "")[:10] == today_str
        ]

        from collections import defaultdict
        _ticket_profit: dict = defaultdict(float)
        _ticket_tt: dict = {}
        for e in closed:
            _ticket_profit[e["ticket"]] += (e["profit"] or 0)
            if e.get("trading_type"):
                _ticket_tt[e["ticket"]] = e["trading_type"]

        _net_trades = list(_ticket_profit.items())
        wins   = [t for t, p in _net_trades if p > 0]
        losses = [t for t, p in _net_trades if p <= 0]

        by_mode: dict = {}
        for tt in ("scalping", "day_trading", "swing"):
            mc = [(t, p) for t, p in _net_trades if _ticket_tt.get(t) == tt]
            if mc:
                mw = sum(1 for _, p in mc if p > 0)
                by_mode[tt] = {"wins": mw, "losses": len(mc) - mw}

        _total_trades = len(_net_trades)
        return {
            "total":        _total_trades,
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / _total_trades * 100, 1) if _total_trades else 0.0,
            "total_profit": round(sum(e["profit"] or 0 for e in closed), 2),
            "by_mode":      by_mode,
            "today_trades": len({e["ticket"] for e in today}),
            "today_pnl":    round(sum(e["profit"] or 0 for e in today), 2),
        }

    def get_closed_merged(
        self,
        account: str = "all",
        trading_type: Optional[str] = None,
        limit: int = 10_000,
        account_login: Optional[int] = None,
    ) -> list[dict]:
        """Return one dict per closed ticket with the true net profit."""
        all_entries = self.get(account=account, limit=limit, account_login=account_login)
        if trading_type:
            all_entries = [e for e in all_entries if e.get("trading_type") == trading_type]

        partial_profit: dict[int, float] = {}
        for e in all_entries:
            if e.get("event") == "partial_close" and e.get("profit") is not None:
                ticket = e.get("ticket")
                if ticket is not None:
                    partial_profit[ticket] = partial_profit.get(ticket, 0.0) + float(e["profit"])

        merged: list[dict] = []
        for e in all_entries:
            if e.get("event") == "close" and e.get("profit") is not None:
                ticket = e.get("ticket")
                if ticket is None:
                    continue
                if ticket is not None:
                    partial_profit[int(ticket)] = partial_profit.get(int(ticket), 0.0) + float(e["profit"])
               
                extra = partial_profit.get(int(ticket), 0.0)
                if extra != 0.0:
                    e = dict(e)
                    e["profit"] = round(float(e["profit"]) + extra, 2)
                merged.append(e)

        return merged

    def get_unclosed_tickets(self) -> list[dict]:
        """Return a list of journal "open" entry dicts that have no matching
        "close" entry for the same ticket."""
        if not self._path.exists():
            return []

        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except Exception:
                return []

        opened: dict[tuple, dict] = {}
        closed_tickets: set[tuple] = set()

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = record.get("ticket")
            account_login = record.get("account_login")
            account_mode = record.get("account_mode")

            if ticket is None:
                continue

            trade_key = (account_mode, account_login, ticket)

            if record.get("event") == "close":
                closed_tickets.add(trade_key)
            elif record.get("event") == "open":
                opened[trade_key] = record

        return [v for k, v in opened.items() if k not in closed_tickets]


# Application-level singleton
trade_journal = TradeJournal()