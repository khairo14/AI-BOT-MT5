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

# Known bug/recovery tickets should stay out of learning.
BUG_TICKETS = {
    "758491108",
    "758491110",
    "758491124",
    "758491143",
    "758572823",
}

# For 758572823, intended-valid journal components only.
KEEP_758572823_PROFITS = {48.07, 20.94}


def backup(path: Path) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, dst)
    print(f"backup: {dst}")


def load_jsonl(path: Path) -> list[dict]:
    rows = []

    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            row = json.loads(line)
            row["_line"] = i
            rows.append(row)

    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            row.pop("_line", None)
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def s(v) -> str:
    return str(v or "").strip()


def ticket(row: dict) -> str:
    return s(row.get("ticket") or row.get("position_id"))


def event(row: dict) -> str:
    return s(row.get("event") or row.get("type")).lower()


def profit(row: dict) -> float | None:
    try:
        if row.get("profit") is None:
            return None
        return round(float(row["profit"]), 2)
    except Exception:
        return None


def symbol_raw(row: dict) -> str:
    return s(row.get("symbol_raw") or row.get("symbol") or row.get("symbol_normalized"))


def symbol_normalized(row: dict) -> str:
    return symbol_raw(row).upper()


def load_memory_index() -> dict[str, dict]:
    rows = load_jsonl(TRADE_MEMORY)
    index = {}

    for row in rows:
        t = ticket(row)
        if not t:
            continue

        login = s(row.get("account_login"))
        if login not in VALID_ACCOUNTS:
            continue

        index[t] = row

    return index


def normalize_from_memory(journal_row: dict, memory_row: dict) -> dict:
    login = s(memory_row.get("account_login"))
    account_type = VALID_ACCOUNTS[login]

    journal_row["account_login"] = int(login)
    journal_row["account_type"] = account_type
    journal_row["mode"] = account_type
    journal_row["user_id"] = memory_row.get("user_id", "default")
    journal_row["strategy"] = journal_row.get("strategy") or memory_row.get("strategy") or journal_row.get("comment") or "unknown"
    journal_row["symbol_raw"] = journal_row.get("symbol_raw") or journal_row.get("symbol") or memory_row.get("symbol_raw")
    journal_row["symbol_normalized"] = (
        journal_row.get("symbol_normalized")
        or memory_row.get("symbol_normalized")
        or symbol_normalized(journal_row)
    )

    journal_row.setdefault("source", f"{account_type}_broker")
    journal_row.setdefault("reconciled", True)

    if ticket(journal_row) in BUG_TICKETS:
        journal_row["bug_contaminated"] = True
        journal_row["learning_valid"] = False
    else:
        journal_row.setdefault("learning_valid", True)

    return journal_row


def main() -> None:
    memory_by_ticket = load_memory_index()
    journal_rows = load_jsonl(TRADE_JOURNAL)

    backup(TRADE_JOURNAL)

    cleaned = []
    removed = []

    seen_exact = set()
    seen_bug_open = set()
    seen_758572823_profit = set()

    for row in journal_rows:
        t = ticket(row)
        ev = event(row)
        p = profit(row)

        mem = memory_by_ticket.get(t)

        # Keep normal rows only if ticket exists in validated trade_memory.
        # Exception: known bug tickets may be kept only as non-learning audit rows.
        if not mem and t not in BUG_TICKETS:
            removed.append(("not_current_valid_account_ticket", t, row.get("_line")))
            continue

        if mem:
            row = normalize_from_memory(row, mem)

        # Bug ticket handling
        if t in BUG_TICKETS:
            row["bug_contaminated"] = True
            row["learning_valid"] = False

            # 758572823 currently has bad aggregated duplicate rows.
            # Keep only open for now if no individual 48.07 / 20.94 rows exist.
            # If individual close rows exist, keep only 48.07 and 20.94.
            if t == "758572823":
                if ev == "open":
                    if t in seen_bug_open:
                        removed.append(("duplicate_bug_open", t, row.get("_line")))
                        continue
                    seen_bug_open.add(t)
                    cleaned.append(row)
                    continue

                if ev == "close":
                    if p not in KEEP_758572823_PROFITS:
                        removed.append(("bad_758572823_close_removed", t, p, row.get("_line")))
                        continue

                    if p in seen_758572823_profit:
                        removed.append(("duplicate_758572823_profit_removed", t, p, row.get("_line")))
                        continue

                    seen_758572823_profit.add(p)
                    cleaned.append(row)
                    continue

                removed.append(("bad_758572823_event_removed", t, ev, row.get("_line")))
                continue

            # Other bug tickets: keep only one open + one exact close.
            if ev == "open":
                if t in seen_bug_open:
                    removed.append(("duplicate_bug_open", t, row.get("_line")))
                    continue
                seen_bug_open.add(t)
                cleaned.append(row)
                continue

            if ev == "close":
                sig = (t, ev, symbol_normalized(row), p, s(row.get("close_time")))
                if sig in seen_exact:
                    removed.append(("duplicate_bug_close", t, p, row.get("_line")))
                    continue
                seen_exact.add(sig)
                cleaned.append(row)
                continue

            removed.append(("bug_other_event_removed", t, ev, row.get("_line")))
            continue

        # Normal validated tickets: keep open/close lifecycle rows, dedupe exact duplicates.
        sig = (
            t,
            ev,
            symbol_normalized(row),
            p,
            s(row.get("open_time")),
            s(row.get("close_time")),
            s(row.get("comment")),
        )

        if sig in seen_exact:
            removed.append(("exact_duplicate_removed", t, ev, p, row.get("_line")))
            continue

        seen_exact.add(sig)
        cleaned.append(row)

    write_jsonl(TRADE_JOURNAL, cleaned)

    print("before:", len(journal_rows))
    print("after :", len(cleaned))
    print("removed:", len(removed))

    print("\nRemoved sample:")
    for x in removed[:120]:
        print(x)

    print("\nDONE")
    print("Next run:")
    print("python tools/audit_reconcile_journals.py")
    print("python tools/audit_trade_journal_events.py")
    print("python tools/audit_mt5_vs_memory.py")


if __name__ == "__main__":
    main()