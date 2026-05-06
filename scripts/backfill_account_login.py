"""
Backfill script to add account_login to existing trade journal and trade memory entries.
Run this once after updating the code.

Usage:
    python scripts/backfill_account_login.py
"""

import json
from pathlib import Path
import shutil
from datetime import datetime

# Paths
TRADE_JOURNAL_PATH = Path("data/trade_journal.jsonl")
TRADE_MEMORY_PATH = Path("ai/data/trade_memory.jsonl")
LIVE_ACCOUNT_LOGIN = 430044251
PAPER_ACCOUNT_LOGIN = 168442709  # XM Demo account


def backfill_journal():
    """Add account_login to all trade_journal.jsonl entries."""
    if not TRADE_JOURNAL_PATH.exists():
        print(f"⚠️ {TRADE_JOURNAL_PATH} not found - skipping")
        return 0

    # Create backup
    backup_path = TRADE_JOURNAL_PATH.with_suffix(".jsonl.bak")
    shutil.copy2(TRADE_JOURNAL_PATH, backup_path)
    print(f"📁 Backup created: {backup_path}")

    updated = 0
    lines = []

    with open(TRADE_JOURNAL_PATH, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                lines.append(line)
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"⚠️ Line {line_num}: JSON decode error - {e}")
                lines.append(line)
                continue

            # Add account_login if missing
            if "account_login" not in entry:
                if entry.get("account_mode") == "live":
                    entry["account_login"] = LIVE_ACCOUNT_LOGIN
                    updated += 1
                elif entry.get("account_mode") == "paper":
                    entry["account_login"] = PAPER_ACCOUNT_LOGIN
                    updated += 1
                else:
                    # Unknown mode - leave as 0
                    entry["account_login"] = 0

            lines.append(json.dumps(entry))

    # Write back
    with open(TRADE_JOURNAL_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ Trade journal: {updated} entries updated")
    return updated


def backfill_memory():
    """Add account_login to all trade_memory.jsonl entries."""
    if not TRADE_MEMORY_PATH.exists():
        print(f"⚠️ {TRADE_MEMORY_PATH} not found - skipping")
        return 0

    # Create backup
    backup_path = TRADE_MEMORY_PATH.with_suffix(".jsonl.bak")
    shutil.copy2(TRADE_MEMORY_PATH, backup_path)
    print(f"📁 Backup created: {backup_path}")

    updated = 0
    lines = []

    with open(TRADE_MEMORY_PATH, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                lines.append(line)
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"⚠️ Line {line_num}: JSON decode error - {e}")
                lines.append(line)
                continue

            # Add account_login if missing
            if "account_login" not in entry:
                if entry.get("mode") == "live":
                    entry["account_login"] = LIVE_ACCOUNT_LOGIN
                    updated += 1
                elif entry.get("mode") == "paper":
                    entry["account_login"] = PAPER_ACCOUNT_LOGIN
                    updated += 1
                else:
                    entry["account_login"] = 0

            lines.append(json.dumps(entry))

    # Write back
    with open(TRADE_MEMORY_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ Trade memory: {updated} entries updated")
    return updated


def main():
    print("=" * 60)
    print("Backfilling account_login to trade files")
    print(f"Live account login: {LIVE_ACCOUNT_LOGIN}")
    print(f"Paper account login: {PAPER_ACCOUNT_LOGIN}")
    print("=" * 60)

    journal_updated = backfill_journal()
    memory_updated = backfill_memory()

    print("\n" + "=" * 60)
    print(f"Summary: Journal: {journal_updated} | Memory: {memory_updated}")
    print("✅ Backfill complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()