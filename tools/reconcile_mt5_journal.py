from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import MetaTrader5 as mt5
from dotenv import load_dotenv

from engine.mt5_client import MT5Client
from engine.order_manager import BOT_MAGIC
from engine.trade_journal import _JOURNAL_PATH, trade_journal
from engine.utils.symbol_utils import normalize_symbol

load_dotenv()

ACCOUNT_LOGIN = 1301109267
ACCOUNT_MODE = "demo"
USER_ID = "default"


def utc_from_ts(ts: float | int | None) -> str:
    return datetime.fromtimestamp(float(ts or 0), tz=timezone.utc).isoformat()


def is_bot_comment(comment: str) -> bool:
    c = str(comment or "").lower().strip()
    return c.startswith(("scalp", "day", "swing"))


def trading_type_from_comment(comment: str) -> str:
    c = str(comment or "").lower().strip()
    if c.startswith("scalp"):
        return "scalping"
    if c.startswith("swing"):
        return "swing"
    if c.startswith("day"):
        return "day_trading"
    return "day_trading"


def strategy_from_comment(comment: str) -> str:
    c = str(comment or "").strip()
    if "|" in c:
        return c.split("|", 1)[1].strip() or c
    return c or "recovered"


def direction_from_open_type(open_type: int) -> str:
    # MT5 deal type: 0 buy, 1 sell
    return "BUY" if int(open_type) == 0 else "SELL"


def deal_net(d: Any) -> float:
    return (
        float(getattr(d, "profit", 0.0) or 0.0)
        + float(getattr(d, "swap", 0.0) or 0.0)
        + float(getattr(d, "commission", 0.0) or 0.0)
        + float(getattr(d, "fee", 0.0) or 0.0)
    )


def build_broker_positions(client: MT5Client) -> dict[int, dict]:
    date_from = datetime(2020, 1, 1, tzinfo=timezone.utc)
    date_to = datetime.now(tz=timezone.utc)

    with client._lock:
        deals = mt5.history_deals_get(date_from, date_to)

    if deals is None:
        raise RuntimeError(f"MT5 history_deals_get returned None: {mt5.last_error()}")

    by_position: dict[int, dict] = {}

    for d in deals:
        position_id = int(getattr(d, "position_id", 0) or 0)
        if position_id <= 0:
            continue

        row = by_position.setdefault(
            position_id,
            {
                "position_id": position_id,
                "open_deals": [],
                "close_deals": [],
                "magic_values": set(),
                "comments": set(),
            },
        )

        magic = int(getattr(d, "magic", 0) or 0)
        comment = str(getattr(d, "comment", "") or "")

        row["magic_values"].add(magic)
        if comment:
            row["comments"].add(comment)

        payload = {
            "ticket": int(getattr(d, "ticket", 0) or 0),
            "order": int(getattr(d, "order", 0) or 0),
            "position_id": position_id,
            "entry": int(getattr(d, "entry", -1)),
            "type": int(getattr(d, "type", -1)),
            "time": utc_from_ts(getattr(d, "time", 0)),
            "time_raw": float(getattr(d, "time", 0) or 0),
            "symbol": str(getattr(d, "symbol", "") or ""),
            "volume": float(getattr(d, "volume", 0.0) or 0.0),
            "price": float(getattr(d, "price", 0.0) or 0.0),
            "profit": float(getattr(d, "profit", 0.0) or 0.0),
            "swap": float(getattr(d, "swap", 0.0) or 0.0),
            "commission": float(getattr(d, "commission", 0.0) or 0.0),
            "fee": float(getattr(d, "fee", 0.0) or 0.0),
            "net": round(deal_net(d), 2),
            "magic": magic,
            "comment": comment,
        }

        if payload["entry"] in (1, 3):  # OUT / INOUT
            row["close_deals"].append(payload)
        else:
            row["open_deals"].append(payload)

    bot_positions: dict[int, dict] = {}

    for pid, row in by_position.items():
        comments = row["comments"]
        magic_values = row["magic_values"]

        is_bot = BOT_MAGIC in magic_values or any(is_bot_comment(c) for c in comments)
        if not is_bot or not row["close_deals"]:
            continue

        row["open_deals"].sort(key=lambda x: x["time_raw"])
        row["close_deals"].sort(key=lambda x: x["time_raw"])

        row["magic_values"] = sorted(list(magic_values))
        row["comments"] = sorted(list(comments))
        row["net_profit"] = round(sum(d["net"] for d in row["close_deals"]), 2)

        bot_positions[pid] = row

    return bot_positions


def existing_journal_closed() -> dict[int, dict]:
    rows = trade_journal.get_closed_merged(
        account=ACCOUNT_MODE,
        account_login=ACCOUNT_LOGIN,
        limit=100_000,
    )
    return {int(r["ticket"]): r for r in rows if r.get("ticket") is not None}


def make_open_row(pos: dict) -> dict:
    open_deal = pos["open_deals"][0]
    open_comment = next((c for c in pos["comments"] if is_bot_comment(c)), open_deal.get("comment", ""))
    trading_type = trading_type_from_comment(open_comment)
    strategy = strategy_from_comment(open_comment)

    return {
        "ticket": pos["position_id"],
        "symbol": open_deal["symbol"],
        "symbol_raw": open_deal["symbol"],
        "symbol_normalized": normalize_symbol(open_deal["symbol"]),
        "direction": direction_from_open_type(open_deal["type"]),
        "volume": open_deal["volume"],
        "entry": open_deal["price"],
        "sl": 0.0,
        "tp": None,
        "tp2": None,
        "tp3": None,
        "profit": None,
        "trading_type": trading_type,
        "strategy": strategy,
        "account_mode": ACCOUNT_MODE,
        "account_login": ACCOUNT_LOGIN,
        "account_type": ACCOUNT_MODE,
        "user_id": USER_ID,
        "comment": strategy,
        "event": "open",
        "open_time": open_deal["time"],
        "close_time": None,
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "confidence": None,
        "expected_price": None,
        "slippage": None,
        "execution_time_ms": None,
        "spread_pips": None,
        "swap": None,
        "commission": None,
        "position_id": pos["position_id"],
        "source": "mt5_reconciliation",
        "recovered": True,
        "learning_valid": False,
    }


def make_close_row(pos: dict, existing: dict | None = None) -> dict:
    open_deal = pos["open_deals"][0]
    final_deal = pos["close_deals"][-1]

    open_comment = next((c for c in pos["comments"] if is_bot_comment(c)), open_deal.get("comment", ""))
    trading_type = (
        existing.get("trading_type")
        if existing
        else trading_type_from_comment(open_comment)
    )
    strategy = (
        existing.get("strategy") or existing.get("comment")
        if existing
        else strategy_from_comment(open_comment)
    )

    total_swap = round(sum(d["swap"] for d in pos["close_deals"]), 2)
    total_commission = round(sum(d["commission"] for d in pos["close_deals"]), 2)
    total_fee = round(sum(d["fee"] for d in pos["close_deals"]), 2)

    return {
        "ticket": pos["position_id"],
        "symbol": open_deal["symbol"],
        "symbol_raw": open_deal["symbol"],
        "symbol_normalized": normalize_symbol(open_deal["symbol"]),
        "direction": existing.get("direction") if existing else direction_from_open_type(open_deal["type"]),
        "volume": open_deal["volume"],
        "entry": existing.get("entry") if existing else open_deal["price"],
        "sl": existing.get("sl") if existing else 0.0,
        "tp": existing.get("tp") if existing else None,
        "tp2": existing.get("tp2") if existing else None,
        "tp3": existing.get("tp3") if existing else None,
        "profit": pos["net_profit"],
        "trading_type": trading_type or "day_trading",
        "strategy": strategy or "recovered",
        "account_mode": ACCOUNT_MODE,
        "account_login": ACCOUNT_LOGIN,
        "account_type": ACCOUNT_MODE,
        "user_id": USER_ID,
        "comment": strategy or "recovered",
        "event": "close",
        "open_time": existing.get("open_time") if existing else open_deal["time"],
        "close_time": final_deal["time"],
        "logged_at": datetime.now(timezone.utc).isoformat(),
        "confidence": existing.get("confidence") if existing else None,
        "expected_price": existing.get("expected_price") if existing else None,
        "slippage": existing.get("slippage") if existing else None,
        "execution_time_ms": existing.get("execution_time_ms") if existing else None,
        "spread_pips": existing.get("spread_pips") if existing else None,
        "swap": total_swap,
        "commission": total_commission,
        "fee": total_fee,
        "position_id": pos["position_id"],
        "source": "mt5_reconciliation",
        "recovered": True,
        "learning_valid": False,
        "reconciliation": {
            "broker_net_profit": pos["net_profit"],
            "close_deal_tickets": [d["ticket"] for d in pos["close_deals"]],
            "reason": "missing" if existing is None else "pnl_correction",
        },
    }


def append_jsonl(rows: list[dict]) -> None:
    with open(_JOURNAL_PATH, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually append reconciliation rows.")
    args = parser.parse_args()

    client = MT5Client()
    if not client.switch_account(ACCOUNT_LOGIN):
        raise RuntimeError(f"Could not connect to account {ACCOUNT_LOGIN}")

    broker = build_broker_positions(client)
    journal = existing_journal_closed()

    missing_ids = sorted(set(broker) - set(journal))

    mismatches = []
    for pid in sorted(set(broker) & set(journal)):
        broker_pnl = round(float(broker[pid]["net_profit"]), 2)
        journal_pnl = round(float(journal[pid].get("profit") or 0.0), 2)
        diff = round(broker_pnl - journal_pnl, 2)
        if abs(diff) >= 0.01:
            mismatches.append((pid, broker_pnl, journal_pnl, diff))

    rows_to_append: list[dict] = []

    for pid in missing_ids:
        pos = broker[pid]
        rows_to_append.append(make_open_row(pos))
        rows_to_append.append(make_close_row(pos, existing=None))

    for pid, broker_pnl, journal_pnl, diff in mismatches:
        pos = broker[pid]
        existing = journal[pid]
        rows_to_append.append(make_close_row(pos, existing=existing))

    print("\n=== RECONCILIATION PLAN ===")
    print(f"Broker bot closed positions: {len(broker)}")
    print(f"Journal closed tickets:      {len(journal)}")
    print(f"Missing positions:           {len(missing_ids)}")
    print(f"PnL mismatches:              {len(mismatches)}")
    print(f"Rows to append:              {len(rows_to_append)}")
    print(f"Apply mode:                  {args.apply}")

    print("\nMissing IDs:")
    print(missing_ids)

    print("\nPnL mismatches:")
    for item in mismatches:
        print(f"position_id={item[0]} broker={item[1]} journal={item[2]} diff={item[3]}")

    out_dir = ROOT / "data" / "reconciliation"
    out_dir.mkdir(parents=True, exist_ok=True)

    plan_file = out_dir / f"reconcile_plan_{ACCOUNT_LOGIN}.json"
    plan_file.write_text(json.dumps(rows_to_append, indent=2, default=str), encoding="utf-8")
    print(f"\nPlan written to: {plan_file}")

    if not args.apply:
        print("\nDry-run only. Nothing written.")
        return

    backup = _JOURNAL_PATH.with_suffix(f".jsonl.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    shutil.copy2(_JOURNAL_PATH, backup)
    append_jsonl(rows_to_append)

    print(f"\nBackup created: {backup}")
    print(f"Appended rows:  {len(rows_to_append)}")


if __name__ == "__main__":
    main()