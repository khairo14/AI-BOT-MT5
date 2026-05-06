from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone
import threading

FILE = Path("data/signal_journal.jsonl")
FILE.parent.mkdir(parents=True, exist_ok=True)


class SignalJournal:
    def __init__(self):
        self._lock = threading.Lock()

    def record(self, entry: dict):
        payload = {
            **entry,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

        with self._lock:
            with open(FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload) + "\n")


signal_journal = SignalJournal()