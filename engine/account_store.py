"""
Account store — persists the active trading account to disk.

config/account_mode.json stores {"login": <account_number>, "type": "<account_type>"}
This survives restarts so the bot reconnects to the last chosen account.

The .env file is the authoritative source at first boot; account_mode.json
takes precedence on subsequent starts.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from loguru import logger

_STORE_PATH = Path(__file__).parent.parent / "config" / "account_mode.json"


def _get_all_accounts() -> list[dict]:
    """Load all configured accounts from .env."""
    demo = json.loads(os.getenv("MT5_DEMO_ACCOUNTS", "[]"))
    live = json.loads(os.getenv("MT5_LIVE_ACCOUNTS", "[]"))
    return demo + live


def load_account() -> dict:
    """
    Return the active account as {login, type}.
    
    Priority:
      1. config/account_mode.json (set by last dashboard switch)
      2. MT5_CURRENT_ACCOUNT env var
      3. First account in the list (safe default)
    """
    if _STORE_PATH.exists():
        try:
            data = json.loads(_STORE_PATH.read_text())
            if "login" in data:
                return data
        except Exception:
            pass

    current = int(os.getenv("MT5_CURRENT_ACCOUNT", "0"))
    for acc in _get_all_accounts():
        if acc["login"] == current:
            return {"login": acc["login"], "type": acc["type"]}
    
    accounts = _get_all_accounts()
    if accounts:
        return {"login": accounts[0]["login"], "type": accounts[0]["type"]}
    return {"login": 0, "type": "unknown"}


def save_account(login: int, account_type: str) -> None:
    """Persist the active account to disk."""
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"login": login, "type": account_type}, indent=2)
    fd, tmp = tempfile.mkstemp(dir=_STORE_PATH.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, _STORE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.environ["MT5_CURRENT_ACCOUNT"] = str(login)
    logger.info(f"Account saved: {login} ({account_type})")


def current_account_login() -> int:
    return load_account().get("login", 0)


def current_account_type() -> str:
    return load_account().get("type", "unknown")


def is_demo() -> bool:
    return current_account_type() == "demo"


def current_mode() -> str:
    """Backward compat — returns 'paper' for demo, 'live' otherwise."""
    return "paper" if is_demo() else "live"