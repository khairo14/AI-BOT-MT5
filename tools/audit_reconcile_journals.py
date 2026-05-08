from __future__ import annotations

import json
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]

TRADE_MEMORY = ROOT / "ai" / "data" / "trade_memory.jsonl"
TRADE_JOURNAL = ROOT / "data" / "trade_journal.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        print(f"[MISSING] {path}")
        return rows

    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                row["_source_file"] = str(path)
                row["_line"] = i
                rows.append(row)
            except Exception as e:
                print(f"[BAD JSON] {path}:{i} {e}")

    return rows


def norm_ticket(row: dict):
    return str(row.get("ticket") or row.get("position_id") or "").strip()


def norm_login(row: dict):
    v = row.get("account_login") or row.get("login")
    return str(v).strip() if v is not None else ""


def norm_symbol(row: dict):
    return str(
        row.get("symbol_normalized")
        or row.get("symbol")
        or row.get("symbol_raw")
        or ""
    ).upper().strip()


def norm_profit(row: dict):
    for k in ("profit", "pnl", "net_profit"):
        if row.get(k) is not None:
            try:
                return round(float(row[k]), 2)
            except Exception:
                return None
    return None


def norm_close_time(row: dict):
    return (
        row.get("close_time")
        or row.get("closed_at")
        or row.get("time_close")
        or row.get("exit_time")
        or ""
    )


def key(row: dict):
    return (
        norm_login(row),
        norm_ticket(row),
        norm_symbol(row),
    )


def summarize(name: str, rows: list[dict]):
    print(f"\n=== {name} ===")
    print("rows:", len(rows))

    accounts = Counter(norm_login(r) or "UNKNOWN" for r in rows)
    modes = Counter(str(r.get("account_type") or r.get("mode") or "UNKNOWN").lower() for r in rows)
    tickets = Counter(norm_ticket(r) or "NO_TICKET" for r in rows)

    print("accounts:", dict(accounts))
    print("modes:", dict(modes))
    print("duplicate tickets:", {k: v for k, v in tickets.items() if v > 1 and k != "NO_TICKET"})


def compare(memory_rows: list[dict], journal_rows: list[dict]):
    memory_by_key = defaultdict(list)
    journal_by_key = defaultdict(list)

    for r in memory_rows:
        memory_by_key[key(r)].append(r)

    for r in journal_rows:
        journal_by_key[key(r)].append(r)

    memory_keys = set(memory_by_key)
    journal_keys = set(journal_by_key)

    print("\n=== CROSS-JOURNAL COMPARISON ===")
    print("memory only:", len(memory_keys - journal_keys))
    print("journal only:", len(journal_keys - memory_keys))
    print("matched keys:", len(memory_keys & journal_keys))

    print("\n--- MEMORY ONLY SAMPLE ---")
    for k in list(memory_keys - journal_keys)[:25]:
        r = memory_by_key[k][0]
        print({
            "key": k,
            "line": r["_line"],
            "profit": norm_profit(r),
            "close_time": norm_close_time(r),
            "outcome": r.get("outcome"),
            "learning_valid": r.get("learning_valid"),
        })

    print("\n--- JOURNAL ONLY SAMPLE ---")
    for k in list(journal_keys - memory_keys)[:25]:
        r = journal_by_key[k][0]
        print({
            "key": k,
            "line": r["_line"],
            "event": r.get("event") or r.get("type"),
            "profit": norm_profit(r),
            "close_time": norm_close_time(r),
        })

    print("\n--- MATCHED PROFIT/CLOSE_TIME MISMATCHES ---")
    mismatches = 0

    for k in sorted(memory_keys & journal_keys):
        m = memory_by_key[k][0]
        j = journal_by_key[k][0]

        mp = norm_profit(m)
        jp = norm_profit(j)

        mt = norm_close_time(m)
        jt = norm_close_time(j)

        profit_bad = mp is not None and jp is not None and abs(mp - jp) > 0.01
        time_bad = bool(mt and jt and mt != jt)

        if profit_bad or time_bad:
            mismatches += 1
            print({
                "key": k,
                "memory_line": m["_line"],
                "journal_line": j["_line"],
                "memory_profit": mp,
                "journal_profit": jp,
                "memory_close_time": mt,
                "journal_close_time": jt,
            })

            if mismatches >= 50:
                print("... stopped after 50 mismatches")
                break

    print("mismatches shown:", mismatches)


def main():
    memory_rows = load_jsonl(TRADE_MEMORY)
    journal_rows = load_jsonl(TRADE_JOURNAL)

    summarize("trade_memory.jsonl", memory_rows)
    summarize("trade_journal.jsonl", journal_rows)

    compare(memory_rows, journal_rows)


if __name__ == "__main__":
    main()