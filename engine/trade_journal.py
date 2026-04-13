"""
Trade Journal — append-only JSONL store for all bot-executed trades.

Every trade placed by the bot (auto or manual approval) is appended here
with its account_mode tag ("paper" / "live"), so the dashboard can show
a unified, filterable history across both accounts.

Storage: data/trade_journal.jsonl (one JSON object per line)

Usage:
    from engine.trade_journal import trade_journal

    trade_journal.log(ticket=12345, symbol="EURUSD", direction="buy",
                      volume=0.1, entry=1.0850, sl=1.0820, tp=1.0910,
                      profit=None, trading_type="scalping",
                      account_mode="paper", comment="EMAScalp")

    entries = trade_journal.get(account="paper", limit=50)
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from loguru import logger

_JOURNAL_PATH = Path(__file__).parent.parent / "data" / "trade_journal.jsonl"
AccountMode = Literal["paper", "live"]


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
        account_mode: AccountMode,
        comment: str = "",
        open_time: Optional[str] = None,
        close_time: Optional[str] = None,
        event: Literal["open", "close"] = "open",
        confidence: Optional[float] = None,
        expected_price: Optional[float] = None,
        slippage: Optional[float] = None,
        execution_time_ms: Optional[int] = None,
        spread_pips: Optional[float] = None,
    ) -> None:
        """Append a trade event to the journal."""
        record = {
            "ticket":       ticket,
            "symbol":       symbol,
            "direction":    direction.upper(),
            "volume":       volume,
            "entry":        entry,
            "sl":           sl,
            "tp":           tp,
            "profit":       profit,
            "trading_type": trading_type,
            "account_mode": account_mode,
            "comment":      comment,
            "event":        event,
            # open events default open_time to now(); close/other events store null
            # so the dashboard always uses the open event as the authoritative source.
            "open_time":    (open_time or datetime.now(tz=timezone.utc).isoformat()) if event == "open" else open_time,
            "close_time":   close_time,
            "logged_at":    datetime.now(tz=timezone.utc).isoformat(),
            "confidence":   confidence,
            # Execution quality metrics (Task 26)
            "expected_price":    expected_price,
            "slippage":          slippage,
            "execution_time_ms": execution_time_ms,
            "spread_pips":       spread_pips,
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
    ) -> list[dict]:
        """
        Read journal entries, newest first.

        Args:
            account:      "paper", "live", or "all"
            trading_type: "scalping", "day_trading", "swing", or None
            event:        "open", "close", or None
            limit:        max entries to return
        """
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
            if trading_type and record.get("trading_type") != trading_type:
                continue
            if event and record.get("event") != event:
                continue
            results.append(record)
            if len(results) >= limit:
                break

        return results

    def stats(self, account: str = "all") -> dict:
        """Return summary stats for the given account filter."""
        from datetime import datetime, timezone
        entries = self.get(account=account, limit=10_000)
        closed  = [e for e in entries if e.get("event") == "close" and e.get("profit") is not None]
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

        wins   = [e for e in closed if (e["profit"] or 0) > 0]
        losses = [e for e in closed if (e["profit"] or 0) <= 0]
        by_mode: dict = {}
        for tt in ("scalping", "day_trading", "swing"):
            mc = [e for e in closed if e.get("trading_type") == tt]
            if mc:
                mw = sum(1 for e in mc if (e["profit"] or 0) > 0)
                by_mode[tt] = {"wins": mw, "losses": len(mc) - mw}
        return {
            "total":        len(closed),
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(closed) * 100, 1),
            "total_profit": round(sum(e["profit"] or 0 for e in closed), 2),
            "by_mode":      by_mode,
            "today_trades": len(today),
            "today_pnl":    round(sum(e["profit"] or 0 for e in today), 2),
        }


    def get_unclosed_tickets(self) -> list[dict]:
        """
        Return a list of journal "open" entry dicts that have no matching
        "close" entry for the same ticket.  Used at startup to recover
        close events that were lost when the server restarted.
        """
        if not self._path.exists():
            return []

        with self._lock:
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except Exception:
                return []

        opened: dict[int, dict] = {}
        closed_tickets: set[int] = set()

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ticket = record.get("ticket")
            if ticket is None:
                continue
            if record.get("event") == "close":
                closed_tickets.add(ticket)
            elif record.get("event") == "open":
                opened[ticket] = record   # last open wins if somehow duplicated

        return [v for k, v in opened.items() if k not in closed_tickets]


# Application-level singleton
trade_journal = TradeJournal()
