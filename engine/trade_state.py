from __future__ import annotations

import json
import threading
from pathlib import Path
from datetime import datetime, timezone

STATE_FILE = Path("data/trade_state.json")
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

class TradeStateStore:
    """
    Persistent trade lifecycle state.

    This is the authoritative runtime state for:
    - TP1
    - TP2
    - BE movement
    - trailing stop
    - partial closes
    - restart recovery
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}
        self._load()

    def _normalize_account_mode(self, account_mode: str) -> str:
        mode = str(account_mode or "live").lower().strip()
        if mode in ("paper", "demo", "test"):
            return "demo"
        return "live" if mode != "demo" else mode

    def _key(self, account_mode: str, account_login: int, ticket: int) -> str:
        mode = self._normalize_account_mode(account_mode)
        return f"{mode}:{int(account_login or 0)}:{int(ticket)}"

    def _load(self):
        if STATE_FILE.exists():
            try:
                self._state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._state = {}

    def _save(self):
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def get(self, account_mode: str, account_login: int, ticket: int) -> dict:
        with self._lock:
            return dict(self._state.get(self._key(account_mode, account_login, ticket), {}))

    def update(self, account_mode: str, account_login: int, ticket: int, **kwargs):
        with self._lock:
            key = self._key(account_mode, account_login, ticket)

            state = self._state.setdefault(key, {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "tp1_hit": False,
                "tp2_hit": False,
                "break_even_moved": False,
                "trailing_active": False,
                "closed": False,
                "partial_closes": [],
            })
            state.update(kwargs)
            state["updated_at"] = datetime.now(timezone.utc).isoformat()

            self._save()

    def mark_partial_close(self, account_mode: str, account_login: int, ticket: int, pct: float, reason: str):
        with self._lock:
            key = self._key(account_mode, account_login, ticket)
            state = self._state.setdefault(key, {})

            partials = state.setdefault("partial_closes", [])
            partials.append({
                "pct": pct,
                "reason": reason,
                "time": datetime.now(timezone.utc).isoformat(),
            })

            self._save()


trade_state_store = TradeStateStore()
