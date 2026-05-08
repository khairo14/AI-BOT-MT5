from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

import MetaTrader5 as mt5


ROOT = Path(__file__).resolve().parents[1]
TRADE_MEMORY = ROOT / "ai" / "data" / "trade_memory.jsonl"


def load_memory() -> list[dict]:
    rows = []

    if not TRADE_MEMORY.exists():
        print(f"[MISSING] {TRADE_MEMORY}")
        return rows

    with TRADE_MEMORY.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                row = json.loads(line)
                row["_line"] = i
                rows.append(row)
            except Exception as e:
                print(f"[BAD JSON] {TRADE_MEMORY}:{i} {e}")

    return rows


def mem_key(row: dict) -> tuple[str, str]:
    return (
        str(row.get("account_login") or ""),
        str(row.get("ticket") or ""),
    )


def main() -> None:
    rows = load_memory()

    print("MEMORY FILE:", TRADE_MEMORY)
    print("MEMORY ROWS:", len(rows))

    memory_by_account_ticket = {
        mem_key(r): r
        for r in rows
        if r.get("ticket")
    }

    memory_by_ticket = {
        str(r.get("ticket") or ""): r
        for r in rows
        if r.get("ticket")
    }

    print("CONNECTING MT5...")

    if not mt5.initialize():
        print("MT5 INIT FAILED")
        print("MT5 last_error:", mt5.last_error())
        return

    account = mt5.account_info()

    if not account:
        print("NO ACCOUNT")
        print("MT5 last_error:", mt5.last_error())
        mt5.shutdown()
        return

    login = account.login

    print("CURRENT LOGIN:", login)
    print("SERVER:", account.server)
    print("BALANCE:", account.balance)
    print("EQUITY:", account.equity)

    date_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    date_to = datetime.now(timezone.utc)

    deals = mt5.history_deals_get(date_from, date_to)

    if deals is None:
        print("NO DEALS")
        print("MT5 last_error:", mt5.last_error())
        mt5.shutdown()
        return

    closed = [
        d for d in deals
        if d.entry == mt5.DEAL_ENTRY_OUT
    ]

    print("MT5 CLOSED DEALS:", len(closed))

    missing_in_memory = []
    account_mismatches = []
    profit_mismatches = []

    for d in closed:
        ticket = str(d.position_id or d.ticket)
        exact_key = (str(login), ticket)

        mem = memory_by_account_ticket.get(exact_key)

        if not mem:
            ticket_mem = memory_by_ticket.get(ticket)

            if ticket_mem:
                account_mismatches.append({
                    "ticket": ticket,
                    "symbol": d.symbol,
                    "mt5_login": login,
                    "memory_login": ticket_mem.get("account_login"),
                    "mt5_profit": round(float(d.profit or 0), 2),
                    "memory_profit": round(float(ticket_mem.get("profit") or 0), 2),
                    "memory_line": ticket_mem.get("_line"),
                    "memory_account_type": ticket_mem.get("account_type"),
                    "memory_mode": ticket_mem.get("mode"),
                })
                continue

            missing_in_memory.append({
                "ticket": ticket,
                "symbol": d.symbol,
                "profit": round(float(d.profit or 0), 2),
                "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat()
                if getattr(d, "time", None)
                else None,
            })
            continue

        memory_profit = round(float(mem.get("profit") or 0), 2)
        mt5_profit = round(float(d.profit or 0), 2)

        if abs(memory_profit - mt5_profit) > 0.01:
            profit_mismatches.append({
                "ticket": ticket,
                "symbol": d.symbol,
                "memory_profit": memory_profit,
                "mt5_profit": mt5_profit,
                "memory_line": mem.get("_line"),
            })

    print("\n=== ACCOUNT MISMATCHES / SAME TICKET FOUND UNDER DIFFERENT LOGIN ===")
    print(len(account_mismatches))
    for x in account_mismatches[:100]:
        print(x)

    print("\n=== MISSING IN MEMORY ===")
    print(len(missing_in_memory))
    for x in missing_in_memory[:100]:
        print(x)

    print("\n=== PROFIT MISMATCHES ===")
    print(len(profit_mismatches))
    for x in profit_mismatches[:100]:
        print(x)

    mt5.shutdown()


if __name__ == "__main__":
    main()