from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import json
from collections import defaultdict
from datetime import datetime, timezone

import MetaTrader5 as mt5
from dotenv import load_dotenv

from engine.mt5_client import MT5Client
from engine.order_manager import BOT_MAGIC
from engine.trade_journal import trade_journal

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

load_dotenv()

ACCOUNT_LOGIN = 1301109267
ACCOUNT_MODE = "demo"

OUT_DEAL_ENTRY_VALUES = {1, 3}  # OUT, INOUT in MT5


def is_bot_comment(comment: str) -> bool:
    c = str(comment or "").lower().strip()
    return c.startswith(("scalp", "day", "swing"))


def main() -> None:
    client = MT5Client()

    if not client.switch_account(ACCOUNT_LOGIN):
        raise RuntimeError(f"Could not switch/connect to MT5 account {ACCOUNT_LOGIN}")

    info = client.get_account_info() or {}
    print("\n=== ACCOUNT ===")
    print(json.dumps(info, indent=2, default=str))

    date_from = datetime(2020, 1, 1, tzinfo=timezone.utc)
    date_to = datetime.now(tz=timezone.utc)

    with client._lock:
        deals = mt5.history_deals_get(date_from, date_to)

    if deals is None:
        print("No MT5 deals returned:", mt5.last_error())
        return

    broker_by_position: dict[int, dict] = {}

    for d in deals:
        position_id = int(getattr(d, "position_id", 0) or 0)
        if position_id <= 0:
            continue

        entry = int(getattr(d, "entry", -1))
        deal_type = int(getattr(d, "type", -1))
        magic = int(getattr(d, "magic", 0) or 0)
        comment = str(getattr(d, "comment", "") or "")

        profit = float(getattr(d, "profit", 0.0) or 0.0)
        swap = float(getattr(d, "swap", 0.0) or 0.0)
        commission = float(getattr(d, "commission", 0.0) or 0.0)
        fee = float(getattr(d, "fee", 0.0) or 0.0)
        net = profit + swap + commission + fee

        row = broker_by_position.setdefault(
            position_id,
            {
                "position_id": position_id,
                "symbol": getattr(d, "symbol", ""),
                "magic_values": set(),
                "comments": set(),
                "deal_tickets": [],
                "open_deals": [],
                "close_deals": [],
                "net_profit": 0.0,
            },
        )

        row["magic_values"].add(magic)
        if comment:
            row["comments"].add(comment)
        row["deal_tickets"].append(int(getattr(d, "ticket", 0) or 0))

        deal_payload = {
            "deal_ticket": int(getattr(d, "ticket", 0) or 0),
            "order": int(getattr(d, "order", 0) or 0),
            "entry": entry,
            "type": deal_type,
            "time": datetime.fromtimestamp(getattr(d, "time", 0), tz=timezone.utc).isoformat(),
            "symbol": getattr(d, "symbol", ""),
            "volume": float(getattr(d, "volume", 0.0) or 0.0),
            "price": float(getattr(d, "price", 0.0) or 0.0),
            "profit": profit,
            "swap": swap,
            "commission": commission,
            "fee": fee,
            "net": net,
            "magic": magic,
            "comment": comment,
        }

        if entry in OUT_DEAL_ENTRY_VALUES:
            row["close_deals"].append(deal_payload)
            row["net_profit"] += net
        else:
            row["open_deals"].append(deal_payload)

    broker_bot = {}
    broker_manual_or_unknown = {}

    for position_id, row in broker_by_position.items():
        magic_values = row["magic_values"]
        comments = row["comments"]

        is_bot = BOT_MAGIC in magic_values or any(is_bot_comment(c) for c in comments)

        clean_row = dict(row)
        clean_row["magic_values"] = sorted(list(magic_values))
        clean_row["comments"] = sorted(list(comments))
        clean_row["net_profit"] = round(clean_row["net_profit"], 2)

        if is_bot and clean_row["close_deals"]:
            broker_bot[position_id] = clean_row
        elif clean_row["close_deals"]:
            broker_manual_or_unknown[position_id] = clean_row

    journal_closed = trade_journal.get_closed_merged(
        account=ACCOUNT_MODE,
        account_login=ACCOUNT_LOGIN,
        limit=100_000,
    )

    journal_by_ticket = {
        int(e["ticket"]): e
        for e in journal_closed
        if e.get("ticket") is not None
    }

    broker_ids = set(broker_bot.keys())
    journal_ids = set(journal_by_ticket.keys())

    missing_in_journal = sorted(broker_ids - journal_ids)
    extra_in_journal = sorted(journal_ids - broker_ids)
    matched = sorted(broker_ids & journal_ids)

    pnl_mismatches = []
    for pid in matched:
        broker_pnl = round(float(broker_bot[pid]["net_profit"]), 2)
        journal_pnl = round(float(journal_by_ticket[pid].get("profit") or 0.0), 2)
        diff = round(broker_pnl - journal_pnl, 2)

        if abs(diff) >= 0.01:
            pnl_mismatches.append(
                {
                    "position_id": pid,
                    "broker_pnl": broker_pnl,
                    "journal_pnl": journal_pnl,
                    "diff": diff,
                    "broker_comments": broker_bot[pid]["comments"],
                    "journal_comment": journal_by_ticket[pid].get("comment"),
                    "journal_strategy": journal_by_ticket[pid].get("strategy"),
                }
            )

    report = {
        "account_login": ACCOUNT_LOGIN,
        "account_mode": ACCOUNT_MODE,
        "bot_magic": BOT_MAGIC,
        "broker_bot_closed_positions": len(broker_bot),
        "broker_bot_total_pnl": round(sum(v["net_profit"] for v in broker_bot.values()), 2),
        "journal_closed_tickets": len(journal_by_ticket),
        "journal_total_pnl": round(sum(float(e.get("profit") or 0.0) for e in journal_by_ticket.values()), 2),
        "missing_in_journal_count": len(missing_in_journal),
        "extra_in_journal_count": len(extra_in_journal),
        "pnl_mismatch_count": len(pnl_mismatches),
        "manual_or_unknown_closed_positions": len(broker_manual_or_unknown),
        "missing_in_journal": [
            broker_bot[pid] for pid in missing_in_journal[:100]
        ],
        "extra_in_journal": [
            journal_by_ticket[pid] for pid in extra_in_journal[:100]
        ],
        "pnl_mismatches": pnl_mismatches[:100],
    }

    out_dir = Path("data/reconciliation")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"mt5_vs_journal_{ACCOUNT_LOGIN}.json"
    out_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== SUMMARY ===")
    for key in [
        "broker_bot_closed_positions",
        "broker_bot_total_pnl",
        "journal_closed_tickets",
        "journal_total_pnl",
        "missing_in_journal_count",
        "extra_in_journal_count",
        "pnl_mismatch_count",
        "manual_or_unknown_closed_positions",
    ]:
        print(f"{key}: {report[key]}")

    print(f"\nReport written to: {out_file}")


if __name__ == "__main__":
    main()