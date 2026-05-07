from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import threading
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

FILE = DATA_DIR / "signal_journal.jsonl"

VALID_SIGNAL_STATUSES = {
    "executed",
    "rejected",
    "blocked",
    "shadow",
    "expired",
    "cancelled",
    "missed",
}

VALID_DECISIONS = {
    "taken",
    "skipped",
    "blocked_by_rl",
    "blocked_by_risk",
    "blocked_by_filter",
    "shadow_only",
}

def normalize_execution_mode(value: Any = None) -> str:
    value = str(value or "live").lower().strip()
    return "demo" if value == "paper" else value

class SignalJournal:
    def __init__(self):
        self._lock = threading.Lock()

    def record(self, entry: dict):

        payload = dict(entry or {})

        payload["direction"] = str(
            payload.get("direction", "")
        ).upper().strip()

        payload["trading_type"] = str(
            payload.get("trading_type")
            or payload.get("mode")
            or ""
        ).lower().strip()

        payload["execution_mode"] = normalize_execution_mode(
            payload.get("execution_mode")
            or payload.get("account_mode")
            or payload.get("mode")
        )

        payload["status"] = str(
            payload.get("status", "shadow")
        ).lower().strip()

        payload["decision"] = str(
            payload.get("decision", "shadow_only")
        ).lower().strip()

        payload["recorded_at"] = datetime.now(timezone.utc).isoformat()

        payload.setdefault("user_id", "default")
        payload.setdefault("account_type", "")
        payload.setdefault("symbol_raw", payload.get("symbol", ""))
        payload.setdefault(
            "symbol_normalized",
            str(payload.get("symbol", "")).upper().strip(),
        )

        payload.setdefault("validated", False)
        payload.setdefault("validated_outcome", None)
        payload.setdefault("future_profit_pips", None)
        payload.setdefault("future_profit_pct", None)

        with self._lock:
            with open(FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")


signal_journal = SignalJournal()