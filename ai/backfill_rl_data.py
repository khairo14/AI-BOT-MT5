"""
One-time backfill script — run once to populate missing RL data files.

Fixes two gaps:
1. rl_history_{trading_type}_paper.jsonl — missing because history logging was
   added after the bootstrap already ran.  We seed each file with a snapshot from
   the Q-table's persisted state so the dashboard shows something immediately.

2. rl_state field in trade_memory.jsonl — missing because older trades were recorded
   before the rl_state capture was wired up.  We recompute the state bucket for
   every trade that lacks it using the same _state() helper the live agent uses.

Usage:
    python -m ai.backfill_rl_data
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

TRADING_TYPES = ["scalping", "day_trading", "swing"]


# ── helpers (mirrored from rl_agent.py) ──────────────────────────────────────

def _wr_bucket(win_rate: float) -> str:
    if win_rate < 0.40:
        return "low"
    if win_rate <= 0.60:
        return "med"
    return "high"


def _conf_bucket(avg_conf: float) -> str:
    if avg_conf < 0.55:
        return "low"
    if avg_conf <= 0.70:
        return "med"
    return "high"


def _session_bucket_from_ts(ts_str: str) -> str:
    """Derive session bucket from an ISO timestamp string."""
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        h = ts.hour
    except Exception:
        return "active"
    if 12 <= h <= 17:
        return "overlap"
    if 7 <= h < 22:
        return "active"
    return "quiet"


def _drawdown_bucket(drawdown_pct: float) -> str:
    if drawdown_pct >= 3.0:
        return "high"
    if drawdown_pct >= 1.5:
        return "med"
    return "low"


def _vol_bucket(vol_pct: float) -> str:
    return "wide" if vol_pct >= 1.0 else "tight"


def compute_state(trade: dict, trailing_win_rate: float, trailing_avg_conf: float) -> str:
    """Compute the RL state for a historical trade using available trade data."""
    entry = trade.get("entry_price", 0.0)
    sl = trade.get("sl_price", 0.0)
    vol_pct = abs(entry - sl) / entry * 100.0 if entry > 0 else 0.5

    ts = trade.get("open_time") or trade.get("recorded_at", "")
    session = _session_bucket_from_ts(ts)

    # For historical backfill we assume 0 drawdown (we don't have intraday equity curve)
    dd = 0.0

    return (
        f"{_wr_bucket(trailing_win_rate)}_{_conf_bucket(trailing_avg_conf)}"
        f"_{session}_{_drawdown_bucket(dd)}_{_vol_bucket(vol_pct)}"
    )


# ── Part 1: Seed RL history files ────────────────────────────────────────────

def backfill_rl_history(mode: str = "paper") -> None:
    print("\n=== Backfilling RL history JSONL files ===")
    for tt in TRADING_TYPES:
        qtable_path = DATA_DIR / f"rl_qtable_{tt}_{mode}.json"
        history_path = DATA_DIR / f"rl_history_{tt}_{mode}.jsonl"

        if not qtable_path.exists():
            print(f"  [{tt}] Q-table not found, skipping.")
            continue

        if history_path.exists():
            # Count existing entries
            count = sum(1 for _ in history_path.open(encoding="utf-8") if _.strip())
            print(f"  [{tt}] History file already exists with {count} entries, skipping.")
            continue

        with open(qtable_path, encoding="utf-8") as f:
            qt = json.load(f)

        n_updates = qt.get("n_updates", 0)
        conf_thresh = qt.get("conf_thresh", 0.55)
        risk_factor = qt.get("risk_factor", 1.0)
        last_state = qt.get("last_state")

        # Write a single bootstrap snapshot representing the current learned state
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trading_type": tt,
            "mode": mode,
            "conf_thresh": conf_thresh,
            "risk_factor": risk_factor,
            "n_updates": n_updates,
            "last_state": last_state,
        }
        with open(history_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

        print(f"  [{tt}] Created history file — seeded with current state: "
              f"conf={conf_thresh:.2f} risk={risk_factor:.2f} n_updates={n_updates}")


# ── Part 2: Backfill rl_state in trade_memory.jsonl ──────────────────────────

def backfill_trade_memory_rl_state() -> None:
    print("\n=== Backfilling rl_state in trade_memory.jsonl ===")
    memory_path = DATA_DIR / "trade_memory.jsonl"

    if not memory_path.exists():
        print("  trade_memory.jsonl not found, skipping.")
        return

    # Load all trades
    trades: list[dict] = []
    with open(memory_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                trades.append(json.loads(line))

    already_has = sum(1 for t in trades if t.get("rl_state"))
    missing = len(trades) - already_has
    print(f"  Total trades: {len(trades)} | Already have rl_state: {already_has} | Missing: {missing}")

    if missing == 0:
        print("  All trades already have rl_state, nothing to do.")
        return

    # Compute trailing win-rate and avg-conf per trading_type as we iterate
    # Use a rolling window of last 20 trades per type for context
    from collections import deque
    trailing: dict[str, dict] = {}
    for tt in TRADING_TYPES:
        trailing[tt] = {"wins": deque(maxlen=20), "confs": deque(maxlen=20)}

    updated = 0
    result_trades: list[dict] = []

    for trade in trades:
        tt = trade.get("trading_type", "day_trading")
        win = trade.get("profit", 0) > 0
        conf = trade.get("confidence", 0.55)

        # Compute trailing stats before adding this trade
        wins_q = trailing[tt]["wins"] if tt in trailing else deque()
        confs_q = trailing[tt]["confs"] if tt in trailing else deque()
        wr = sum(wins_q) / len(wins_q) if wins_q else 0.5
        avg_conf = sum(confs_q) / len(confs_q) if confs_q else 0.55

        if not trade.get("rl_state"):
            trade["rl_state"] = compute_state(trade, wr, avg_conf)
            updated += 1

        # Update trailing stats after processing
        if tt in trailing:
            trailing[tt]["wins"].append(1 if win else 0)
            trailing[tt]["confs"].append(conf)

        result_trades.append(trade)

    # Write back
    backup_path = memory_path.with_suffix(".jsonl.rl_backfill_bak")
    if not backup_path.exists():
        import shutil
        shutil.copy2(memory_path, backup_path)
        print(f"  Backup saved to {backup_path.name}")

    with open(memory_path, "w", encoding="utf-8") as f:
        for t in result_trades:
            f.write(json.dumps(t) + "\n")

    print(f"  Done — backfilled rl_state for {updated} trades.")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    backfill_rl_history(mode="paper")
    backfill_trade_memory_rl_state()
    print("\nBackfill complete.")
