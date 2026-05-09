from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "data" / "trade_journal.jsonl"
REFERENCE = ROOT / "data" / "reconciliation" / "old_trade_journal_reference.jsonl"

FIELDS = ("sl", "tp", "tp2", "tp3", "confidence", "expected_price")


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main() -> None:
    if not CURRENT.exists():
        raise FileNotFoundError(CURRENT)

    if not REFERENCE.exists():
        raise FileNotFoundError(REFERENCE)

    reference_rows = load_jsonl(REFERENCE)
    current_rows = load_jsonl(CURRENT)

    reference_by_ticket: dict[int, dict] = {}

    for row in reference_rows:
        ticket = row.get("ticket")
        if ticket is None:
            continue

        ticket = int(ticket)

        has_sltp = any(row.get(field) not in (None, "", 0, 0.0) for field in ("sl", "tp", "tp2", "tp3"))

        if has_sltp:
            reference_by_ticket[ticket] = row

    patched = 0
    patched_tickets = set()

    for row in current_rows:
        if row.get("source") != "mt5_reconciliation":
            continue

        ticket = row.get("ticket")
        if ticket is None:
            continue

        ticket = int(ticket)
        ref = reference_by_ticket.get(ticket)

        if not ref:
            continue

        changed = False

        for field in FIELDS:
            ref_value = ref.get(field)

            if ref_value not in (None, "", 0, 0.0):
                if row.get(field) != ref_value:
                    row[field] = ref_value
                    changed = True

        if changed:
            row["sltp_recovered"] = True
            row["sltp_source"] = "old_trade_journal_reference"
            patched += 1
            patched_tickets.add(ticket)

    backup = CURRENT.with_suffix(f".jsonl.backup_sltp_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(CURRENT, backup)

    with open(CURRENT, "w", encoding="utf-8") as f:
        for row in current_rows:
            f.write(json.dumps(row, default=str) + "\n")

    print("Backup:", backup)
    print("Patched rows:", patched)
    print("Patched tickets:", sorted(patched_tickets))


if __name__ == "__main__":
    main()