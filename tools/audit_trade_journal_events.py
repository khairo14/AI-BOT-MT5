from __future__ import annotations

import json
from pathlib import Path
from collections import defaultdict, Counter


ROOT = Path(__file__).resolve().parents[1]
TRADE_JOURNAL = ROOT / "data" / "trade_journal.jsonl"


def load_rows():
    rows = []

    with TRADE_JOURNAL.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                row = json.loads(line)
                row["_line"] = i
                rows.append(row)
            except Exception as e:
                print(f"[BAD JSON] line={i} err={e}")

    return rows


def ticket_of(r):
    return str(r.get("ticket") or "").strip()


def event_of(r):
    return (
        r.get("event")
        or r.get("type")
        or r.get("status")
        or "unknown"
    ).lower()


def summarize_ticket(ticket, rows):
    print("\n================================================")
    print("TICKET:", ticket)
    print("COUNT :", len(rows))

    for r in rows:
        print({
            "line": r["_line"],
            "event": event_of(r),
            "account_login": r.get("account_login"),
            "account_type": r.get("account_type"),
            "symbol": r.get("symbol"),
            "profit": r.get("profit"),
            "close_time": r.get("close_time"),
            "comment": r.get("comment"),
        })


def main():
    rows = load_rows()

    by_ticket = defaultdict(list)

    for r in rows:
        t = ticket_of(r)
        if not t:
            continue
        by_ticket[t].append(r)

    counts = Counter({
        t: len(v)
        for t, v in by_ticket.items()
    })

    print("TOTAL ROWS:", len(rows))
    print("TOTAL TICKETS:", len(by_ticket))

    print("\n=== HIGH DUPLICATE TICKETS ===")

    high = sorted(
        counts.items(),
        key=lambda x: x[1],
        reverse=True
    )

    for ticket, count in high[:25]:
        if count <= 2:
            continue

        print(ticket, "->", count)

    print("\n=== CONTAMINATED RECOVERY TICKETS ===")

    targets = [
        "758491108",
        "758491110",
        "758491124",
        "758491143",
        "758572823",
    ]

    for t in targets:
        if t in by_ticket:
            summarize_ticket(t, by_ticket[t])


if __name__ == "__main__":
    main()