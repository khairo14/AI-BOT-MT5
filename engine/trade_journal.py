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

def normalize_account_mode(
    value: str | None,
    account_login: int | None = None,
) -> str:
    value = str(value or "").lower().strip()
    
    if value in ("all", "demo", "live"):
        return value

    # Fallback reconciliation for known accounts
    if str(account_login or "") == "1301109267":
        return "demo"

    if str(account_login or "") == "430044251":
        return "live"

    # Default safe fallback
    return "demo"

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
            "account_mode": normalize_account_mode(account_mode, account_login),
            "account_login": account_login,
            "account_type": normalize_account_mode(account_type or account_mode, account_login),
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
        limit: int = 5000,
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
        account = normalize_account_mode(account, account_login)
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
            record_mode = normalize_account_mode(
                record.get("account_mode") or record.get("account_type"),
                record.get("account_login"),
            )

            record["account_mode"] = record_mode
            record["account_type"] = record_mode

            if account != "all" and record_mode != account:
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
        """Return summary stats for the given account filter, deduped by ticket.

        Latest close row is authoritative. partial_close rows are only used for
        legacy tickets that do not have a final close row.
        """
        entries = self.get(account=account, limit=100_000, account_login=account_login)

        events = [
            e for e in entries
            if e.get("event") in ("close", "partial_close") and e.get("profit") is not None
        ]

        if not events:
            return {
                "total": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_profit": 0.0,
                "today_trades": 0,
                "today_pnl": 0.0,
                "by_mode": {},
            }

        today_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

        from collections import defaultdict

        ticket_profit: dict[int, float] = {}
        ticket_today_profit: dict[int, float] = {}
        ticket_trading_type: dict[int, str] = {}
        seen_close_tickets: set[int] = set()

        legacy_partial_profit: dict[int, float] = defaultdict(float)
        legacy_partial_today_profit: dict[int, float] = defaultdict(float)
        seen_partial_keys: set[tuple] = set()

        # self.get() returns newest first.
        # First close row per ticket is latest/authoritative.
        for e in events:
            ticket_raw = e.get("ticket")
            if ticket_raw is None:
                continue

            ticket = int(ticket_raw)
            event = e.get("event")
            profit = float(e.get("profit") or 0.0)

            if e.get("trading_type") and ticket not in ticket_trading_type:
                ticket_trading_type[ticket] = e["trading_type"]

            is_today = (e.get("close_time") or e.get("logged_at") or "")[:10] == today_str

            if event == "close":
                if ticket in seen_close_tickets:
                    continue

                seen_close_tickets.add(ticket)
                ticket_profit[ticket] = profit

                if is_today:
                    ticket_today_profit[ticket] = profit

            elif event == "partial_close":
                partial_key = (
                    ticket,
                    e.get("close_time") or e.get("logged_at"),
                    round(profit, 2),
                )

                if partial_key in seen_partial_keys:
                    continue

                seen_partial_keys.add(partial_key)
                legacy_partial_profit[ticket] += profit

                if is_today:
                    legacy_partial_today_profit[ticket] += profit

        # Legacy fallback: only count partial_close-only tickets if no close row exists.
        for ticket, profit in legacy_partial_profit.items():
            if ticket in seen_close_tickets:
                continue

            ticket_profit[ticket] = profit

            if ticket in legacy_partial_today_profit:
                ticket_today_profit[ticket] = legacy_partial_today_profit[ticket]

        net_trades = list(ticket_profit.items())

        wins = [ticket for ticket, profit in net_trades if profit > 0]
        losses = [ticket for ticket, profit in net_trades if profit <= 0]

        by_mode: dict = {}
        for tt in ("scalping", "day_trading", "swing"):
            mode_trades = [
                (ticket, profit)
                for ticket, profit in net_trades
                if ticket_trading_type.get(ticket) == tt
            ]

            if mode_trades:
                mode_wins = sum(1 for _, profit in mode_trades if profit > 0)
                by_mode[tt] = {
                    "wins": mode_wins,
                    "losses": len(mode_trades) - mode_wins,
                }

        total_trades = len(net_trades)

        return {
            "total": total_trades,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / total_trades * 100, 1) if total_trades else 0.0,
            "total_profit": round(sum(ticket_profit.values()), 2),
            "by_mode": by_mode,
            "today_trades": len(ticket_today_profit),
            "today_pnl": round(sum(ticket_today_profit.values()), 2),
        }
    
    def get_closed_merged(
        self,
        account: str = "all",
        trading_type: Optional[str] = None,
        limit: int = 10_000,
        account_login: Optional[int] = None,
    ) -> list[dict]:
        """Return one dict per closed ticket with latest close row as authority."""
        all_entries = self.get(account=account, limit=limit, account_login=account_login)

        if trading_type:
            all_entries = [
                e for e in all_entries
                if e.get("trading_type") == trading_type
            ]

        partial_profit: dict[int, float] = {}
        seen_partial_keys: set[tuple] = set()

        for e in all_entries:
            if e.get("event") == "partial_close" and e.get("profit") is not None:
                ticket = e.get("ticket")
                if ticket is None:
                    continue

                profit = float(e.get("profit") or 0.0)
                partial_key = (
                    int(ticket),
                    e.get("close_time") or e.get("logged_at"),
                    round(profit, 2),
                )

                if partial_key in seen_partial_keys:
                    continue

                seen_partial_keys.add(partial_key)
                partial_profit[int(ticket)] = partial_profit.get(int(ticket), 0.0) + profit

        merged: list[dict] = []
        seen_close_tickets: set[int] = set()

        # self.get() returns newest first.
        # First close row per ticket is authoritative.
        for e in all_entries:
            if e.get("event") == "close" and e.get("profit") is not None:
                ticket = e.get("ticket")

                if ticket is None:
                    continue

                ticket = int(ticket)

                if ticket in seen_close_tickets:
                    continue

                seen_close_tickets.add(ticket)

                merged_entry = dict(e)

                # Normal current architecture: final close already contains full net PnL.
                # Legacy fallback only: if older rows have partial_close events, add them
                # unless this close row is an explicit reconciliation correction.
                if not merged_entry.get("recovered") and not merged_entry.get("reconciliation"):
                    merged_entry["profit"] = round(
                        float(merged_entry.get("profit") or 0.0)
                        + partial_profit.get(ticket, 0.0),
                        2,
                    )
                else:
                    merged_entry["profit"] = round(float(merged_entry.get("profit") or 0.0), 2)

                merged.append(merged_entry)

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