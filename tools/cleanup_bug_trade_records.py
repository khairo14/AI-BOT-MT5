from __future__ import annotations

import json
import shutil
from pathlib import Path
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]

TRADE_MEMORY = ROOT / "ai" / "data" / "trade_memory.jsonl"
TRADE_JOURNAL = ROOT / "data" / "trade_journal.jsonl"

VALID_ACCOUNTS = {
    "1301109267": "demo",
    "430044251": "live",
}

BUG_TICKETS = {
    "758491108",
    "758491110",
    "758491124",
    "758491143",
    "758572823",
}


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows

    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                rows.append(json.loads(line))
            except Exception as e:
                print(f"[BAD JSON] {path}:{i} {e}")

    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def ticket(row: dict) -> str:
    return str(row.get("ticket") or row.get("position_id") or "").strip()


def account_login(row: dict) -> str:
    return str(row.get("account_login") or row.get("login") or "").strip()


def normalize_account_type(row: dict) -> str:
    login = account_login(row)
    expected = VALID_ACCOUNTS.get(login)

    if expected:
        row["account_login"] = int(login)
        row["account_type"] = expected

    return expected or ""


def clean_trade_memory(rows: list[dict]) -> list[dict]:
    cleaned = []
    removed = []

    for row in rows:
        t = ticket(row)
        login = account_login(row)

        if t in BUG_TICKETS:
            removed.append(("bug_ticket", t, login))
            continue

        if login not in VALID_ACCOUNTS:
            removed.append(("invalid_account", t, login))
            continue

        normalize_account_type(row)

        row["learning_valid"] = True
        row["reconciled"] = True
        row.setdefault("source", "broker")

        cleaned.append(row)

    print("\n=== trade_memory cleanup ===")
    print("before:", len(rows))
    print("after :", len(cleaned))
    print("removed:", len(removed))

    for x in removed[:50]:
        print(x)

    return cleaned


def journal_event(row: dict) -> str:
    return str(row.get("event") or row.get("type") or "").lower().strip()


def close_signature(row: dict) -> tuple:
    return (
        ticket(row),
        row.get("symbol"),
        row.get("profit"),
        row.get("close_time"),
        row.get("comment"),
    )


def clean_trade_journal(rows: list[dict]) -> list[dict]:
    cleaned = []
    removed = []

    seen_bug_closes = set()

    for row in rows:
        t = ticket(row)
        event = journal_event(row)

        if t not in BUG_TICKETS:
            cleaned.append(row)
            continue

        # Keep one open event for audit trail.
        if event == "open":
            if any(ticket(r) == t and journal_event(r) == "open" for r in cleaned):
                removed.append(("duplicate_bug_open", t))
                continue

            cleaned.append(row)
            continue

        # Keep only one identical close replay.
        if event == "close":
            sig = close_signature(row)

            if sig in seen_bug_closes:
                removed.append(("duplicate_bug_close", t, row.get("profit"), row.get("close_time")))
                continue

            seen_bug_closes.add(sig)

            # Keep first close record for audit only.
            cleaned.append(row)
            continue

        removed.append(("bug_other_event", t, event))

    print("\n=== trade_journal cleanup ===")
    print("before:", len(rows))
    print("after :", len(cleaned))
    print("removed:", len(removed))

    for x in removed[:80]:
        print(x)

    return cleaned


def main() -> None:
    memory_rows = load_jsonl(TRADE_MEMORY)
    journal_rows = load_jsonl(TRADE_JOURNAL)

    if TRADE_MEMORY.exists():
        print("backup:", backup(TRADE_MEMORY))

    if TRADE_JOURNAL.exists():
        print("backup:", backup(TRADE_JOURNAL))

    cleaned_memory = clean_trade_memory(memory_rows)
    cleaned_journal = clean_trade_journal(journal_rows)

    write_jsonl(TRADE_MEMORY, cleaned_memory)
    write_jsonl(TRADE_JOURNAL, cleaned_journal)

    print("\nDONE")
    print("Re-run audits after cleanup:")
    print("python tools/audit_reconcile_journals.py")
    print("python tools/audit_trade_journal_events.py")
    print("python tools/audit_mt5_vs_memory.py")


if __name__ == "__main__":
    main()