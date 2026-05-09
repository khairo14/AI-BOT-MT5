from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.trade_journal import trade_journal

MEMORY_FILE = ROOT / "ai" / "data" / "trade_memory.jsonl"
ACCOUNT_LOGIN = 1301109267
ACCOUNT_MODE = "demo"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows

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


def make_memory_row(j: dict) -> dict:
    profit = float(j.get("profit") or 0.0)

    return {
        "ticket": int(j["ticket"]),
        "symbol": j.get("symbol", ""),
        "symbol_raw": j.get("symbol_raw") or j.get("symbol", ""),
        "symbol_normalized": j.get("symbol_normalized") or j.get("symbol", ""),
        "strategy": j.get("strategy") or j.get("comment") or "recovered",
        "trading_type": j.get("trading_type") or "day_trading",
        "direction": str(j.get("direction") or "").upper(),
        "confidence": float(j.get("confidence") or 0.5),
        "entry_price": float(j.get("entry") or 0.0),
        "close_price": float(j.get("close_price") or j.get("entry") or 0.0),
        "sl_price": float(j.get("sl") or 0.0),
        "tp_price": float(j.get("tp") or 0.0),
        "volume": float(j.get("volume") or 0.0),
        "profit": profit,
        "profit_pips": 0.0,
        "profit_pct": 0.0,
        "outcome": "tp_hit" if profit > 0 else "sl_hit",
        "open_time": j.get("open_time") or "",
        "close_time": j.get("close_time") or j.get("logged_at") or "",
        "duration_mins": 0.0,
        "mode": j.get("trading_type") or "day_trading",
        "account_login": ACCOUNT_LOGIN,
        "account_type": ACCOUNT_MODE,
        "execution_mode": ACCOUNT_MODE,
        "source": "mt5_reconciliation",
        "user_id": j.get("user_id") or "default",
        "learning_valid": False,
        "validation_errors": ["recovered_from_mt5_pending_review"],
        "extra": {
            "source": "mt5_reconciliation",
            "recovered": True,
            "position_id": j.get("position_id") or j.get("ticket"),
            "broker_net_profit": profit,
            "journal_source": j.get("source"),
        },
        "recorded_at": datetime.utcnow().isoformat() + "Z",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    memory_rows = load_jsonl(MEMORY_FILE)
    memory_tickets = {int(r["ticket"]) for r in memory_rows if r.get("ticket") is not None}

    journal_rows = trade_journal.get_closed_merged(
        account=ACCOUNT_MODE,
        account_login=ACCOUNT_LOGIN,
        limit=100_000,
    )

    recovered_journal = [
        r for r in journal_rows
        if r.get("source") == "mt5_reconciliation"
        and r.get("recovered") is True
    ]

    missing_memory = [
        r for r in recovered_journal
        if int(r["ticket"]) not in memory_tickets
    ]

    rows_to_append = [make_memory_row(r) for r in missing_memory]

    print("\n=== TRADE MEMORY RECONCILIATION ===")
    print(f"Memory rows existing:         {len(memory_rows)}")
    print(f"Recovered journal rows found: {len(recovered_journal)}")
    print(f"Missing memory rows:          {len(rows_to_append)}")
    print(f"Apply mode:                   {args.apply}")

    print("\nTickets to append:")
    print([r["ticket"] for r in rows_to_append])

    plan_dir = ROOT / "data" / "reconciliation"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_file = plan_dir / f"trade_memory_reconcile_plan_{ACCOUNT_LOGIN}.json"
    plan_file.write_text(json.dumps(rows_to_append, indent=2, default=str), encoding="utf-8")

    print(f"\nPlan written to: {plan_file}")

    if not args.apply:
        print("Dry-run only. Nothing written.")
        return

    backup = MEMORY_FILE.with_suffix(f".jsonl.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(MEMORY_FILE, backup)

    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        for row in rows_to_append:
            f.write(json.dumps(row, default=str) + "\n")

    print(f"Backup created: {backup}")
    print(f"Appended rows:  {len(rows_to_append)}")


if __name__ == "__main__":
    main()