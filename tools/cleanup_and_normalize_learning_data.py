from __future__ import annotations

import json
import shutil
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]

TRADE_MEMORY = ROOT / "ai" / "data" / "trade_memory.jsonl"
TRADE_JOURNAL = ROOT / "data" / "trade_journal.jsonl"

CURRENT_DEMO_LOGIN = "1301109267"
VALID_LIVE_LOGIN = "430044251"

VALID_ACCOUNTS = {
    CURRENT_DEMO_LOGIN: "demo",
    VALID_LIVE_LOGIN: "live",
}

# old demo already confirmed same MT5 lineage, migrate to current demo
ACCOUNT_MIGRATIONS = {
    "168442709": CURRENT_DEMO_LOGIN,
}

BUG_TICKETS_REMOVE_FROM_MEMORY = {
    "758491108",
    "758491110",
    "758491124",
    "758491143",
    "758572823",
}

# For journal only: 758572823 intended valid events
BUG_758572823_KEEP_CLOSE_PROFITS = {48.07, 20.94}


def backup(path: Path) -> None:
    if not path.exists():
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, dst)
    print(f"backup: {dst}")


def load_jsonl(path: Path) -> list[dict]:
    rows = []

    if not path.exists():
        print(f"[missing] {path}")
        return rows

    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue

            try:
                row = json.loads(line)
                row["_line"] = i
                rows.append(row)
            except Exception as e:
                print(f"[bad json] {path}:{i} {e}")

    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            row.pop("_line", None)
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def s(value) -> str:
    return str(value or "").strip()


def ticket(row: dict) -> str:
    return s(row.get("ticket") or row.get("position_id"))


def account_login(row: dict) -> str:
    login = s(row.get("account_login") or row.get("login"))

    if login in ACCOUNT_MIGRATIONS:
        login = ACCOUNT_MIGRATIONS[login]

    return login


def symbol_raw(row: dict) -> str:
    return s(row.get("symbol_raw") or row.get("symbol") or row.get("symbol_normalized"))


def symbol_normalized(row: dict) -> str:
    return s(row.get("symbol_normalized") or row.get("symbol") or row.get("symbol_raw")).upper()


def infer_source(account_type: str) -> str:
    if account_type == "live":
        return "live_broker"
    if account_type == "demo":
        return "demo_broker"
    return "unknown"


def normalize_common_fields(row: dict, *, force_learning_valid: bool | None = None) -> dict:
    login = account_login(row)
    account_type = VALID_ACCOUNTS.get(login, s(row.get("account_type") or row.get("mode")).lower())

    if login:
        row["account_login"] = int(login) if login.isdigit() else login

    row["account_type"] = account_type
    row["symbol_raw"] = symbol_raw(row)
    row["symbol_normalized"] = symbol_normalized(row)

    # paper == demo compatibility
    if s(row.get("mode")).lower() == "paper":
        row["mode"] = "demo"

    row.setdefault("user_id", "default")
    row.setdefault("strategy", s(row.get("comment")) or "unknown")
    row.setdefault("source", infer_source(account_type))
    row.setdefault("reconciled", True)

    if force_learning_valid is not None:
        row["learning_valid"] = force_learning_valid
    else:
        row.setdefault("learning_valid", True)

    return row


def clean_memory(rows: list[dict]) -> list[dict]:
    out = []
    removed = []

    for row in rows:
        t = ticket(row)
        login = account_login(row)

        if t in BUG_TICKETS_REMOVE_FROM_MEMORY:
            removed.append(("bug_ticket_removed_from_memory", t, login, row.get("_line")))
            continue

        if login not in VALID_ACCOUNTS:
            removed.append(("invalid_account_removed_from_memory", t, login, row.get("_line")))
            continue

        row = normalize_common_fields(row, force_learning_valid=True)
        row["reconciled"] = True

        out.append(row)

    print("\n=== trade_memory ===")
    print("before:", len(rows))
    print("after :", len(out))
    print("removed:", len(removed))
    for x in removed[:80]:
        print(x)

    return out


def event(row: dict) -> str:
    return s(row.get("event") or row.get("type")).lower()


def profit(row: dict) -> float | None:
    try:
        if row.get("profit") is None:
            return None
        return round(float(row.get("profit")), 2)
    except Exception:
        return None


def journal_close_sig(row: dict) -> tuple:
    return (
        ticket(row),
        symbol_normalized(row),
        profit(row),
        s(row.get("close_time")),
        s(row.get("comment")),
    )


def clean_journal(rows: list[dict]) -> list[dict]:
    out = []
    removed = []
    seen_exact_bug_closes = set()
    seen_bug_opens = set()

    for row in rows:
        t = ticket(row)
        ev = event(row)

        # Normalize account fields when possible, but do not remove old journal audit rows blindly.
        login = account_login(row)
        if login in VALID_ACCOUNTS:
            row = normalize_common_fields(row, force_learning_valid=None)
        elif login:
            row["account_login"] = int(login) if login.isdigit() else login

        if t not in BUG_TICKETS_REMOVE_FROM_MEMORY:
            out.append(row)
            continue

        if ev == "open":
            if t in seen_bug_opens:
                removed.append(("duplicate_bug_open_removed", t, row.get("_line")))
                continue
            seen_bug_opens.add(t)
            row["bug_contaminated"] = True
            row["learning_valid"] = False
            out.append(row)
            continue

        if ev == "close":
            p = profit(row)

            if t == "758572823":
                if p not in BUG_758572823_KEEP_CLOSE_PROFITS:
                    removed.append(("invalid_restart_tp_removed", t, p, row.get("_line")))
                    continue

                # keep only one 48.07 and one 20.94
                sig = (t, p)
                if sig in seen_exact_bug_closes:
                    removed.append(("duplicate_kept_profit_removed", t, p, row.get("_line")))
                    continue

                seen_exact_bug_closes.add(sig)
                row["bug_contaminated"] = True
                row["learning_valid"] = False
                row["valid_strategy_profit_component"] = p
                out.append(row)
                continue

            # Other bug tickets: keep only one exact close replay.
            sig = journal_close_sig(row)
            if sig in seen_exact_bug_closes:
                removed.append(("duplicate_bug_close_removed", t, p, row.get("_line")))
                continue

            seen_exact_bug_closes.add(sig)
            row["bug_contaminated"] = True
            row["learning_valid"] = False
            out.append(row)
            continue

        removed.append(("bug_other_event_removed", t, ev, row.get("_line")))

    print("\n=== trade_journal ===")
    print("before:", len(rows))
    print("after :", len(out))
    print("removed:", len(removed))
    for x in removed[:120]:
        print(x)

    return out


def main() -> None:
    memory_rows = load_jsonl(TRADE_MEMORY)
    journal_rows = load_jsonl(TRADE_JOURNAL)

    backup(TRADE_MEMORY)
    backup(TRADE_JOURNAL)

    cleaned_memory = clean_memory(memory_rows)
    cleaned_journal = clean_journal(journal_rows)

    write_jsonl(TRADE_MEMORY, cleaned_memory)
    write_jsonl(TRADE_JOURNAL, cleaned_journal)

    print("\nDONE")
    print("Next run:")
    print("python tools/audit_reconcile_journals.py")
    print("python tools/audit_trade_journal_events.py")
    print("python tools/audit_mt5_vs_memory.py")


if __name__ == "__main__":
    main()