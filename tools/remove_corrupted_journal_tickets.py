from __future__ import annotations

import json
import shutil
from pathlib import Path
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]

TRADE_JOURNAL = ROOT / "data" / "trade_journal.jsonl"

REMOVE_TICKETS = {
    "758491108",
    "758491110",
    "758491124",
    "758491143",
    "758572823",
}


def load_jsonl(path: Path):
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


def write_jsonl(path: Path, rows: list[dict]):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            row.pop("_line", None)
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def backup(path: Path):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = path.with_suffix(path.suffix + f".bak_{stamp}")
    shutil.copy2(path, dst)
    print("backup:", dst)


def ticket(row: dict) -> str:
    return str(
        row.get("ticket")
        or row.get("position_id")
        or ""
    ).strip()


def main():
    backup(TRADE_JOURNAL)

    rows = load_jsonl(TRADE_JOURNAL)

    cleaned = []
    removed = []

    for row in rows:
        t = ticket(row)

        if t in REMOVE_TICKETS:
            removed.append((t, row.get("_line")))
            continue

        cleaned.append(row)

    write_jsonl(TRADE_JOURNAL, cleaned)

    print("before:", len(rows))
    print("after :", len(cleaned))
    print("removed:", len(removed))

    print("\nRemoved:")
    for x in removed:
        print(x)

    print("\nRe-run:")
    print("python tools/audit_reconcile_journals.py")
    print("python tools/audit_trade_journal_events.py")


if __name__ == "__main__":
    main()