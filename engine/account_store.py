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
from typing import Any

from loguru import logger

_STORE_PATH = Path(__file__).parent.parent / "config" / "account_mode.json"
_VALID_ACCOUNT_TYPES = {"demo", "live"}


def normalize_account_type(value: Any) -> str:
    """Return only demo/live/unknown. No paper fallback exists anymore."""
    value = str(value or "").lower().strip()
    return value if value in _VALID_ACCOUNT_TYPES else "unknown"


def _get_all_accounts() -> list[dict]:
    """Load all configured accounts from .env."""
    try:
        demo = json.loads(os.getenv("MT5_DEMO_ACCOUNTS", "[]") or "[]")
    except json.JSONDecodeError:
        logger.warning("Invalid MT5_DEMO_ACCOUNTS JSON; treating as empty")
        demo = []

    try:
        live = json.loads(os.getenv("MT5_LIVE_ACCOUNTS", "[]") or "[]")
    except json.JSONDecodeError:
        logger.warning("Invalid MT5_LIVE_ACCOUNTS JSON; treating as empty")
        live = []

    return list(demo or []) + list(live or [])


def resolve_account_type(login: int | str, fallback: Any = "unknown") -> str:
    """
    Resolve account type dynamically from configured accounts.
    Returns demo/live when known, otherwise normalized fallback or unknown.
    """
    try:
        target_login = int(login)
    except (TypeError, ValueError):
        return normalize_account_type(fallback)

    for acc in _get_all_accounts():
        try:
            acc_login = int(acc.get("login", 0))
        except (TypeError, ValueError):
            continue

        if acc_login != target_login:
            continue

        account_type = normalize_account_type(
            acc.get("type")
            or acc.get("account_type")
            or acc.get("account_mode")
            or acc.get("mode")
        )
        if account_type != "unknown":
            return account_type

        server = str(acc.get("server") or "").lower()
        if "demo" in server:
            return "demo"
        if server:
            return "live"

        return normalize_account_type(fallback)

    return normalize_account_type(fallback)


def load_account() -> dict:
    """
    Return the active account as {login, type}.

    Priority:
      1. config/account_mode.json (set by last dashboard switch)
      2. MT5_CURRENT_ACCOUNT env var
      3. First account in the list (safe default)

    If account_mode.json exists but its type is missing/unknown, repair it from
    the configured accounts instead of returning unknown to the dashboard.
    """
    if _STORE_PATH.exists():
        try:
            data = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
            login = int(data.get("login", 0))
            if login:
                stored_type = normalize_account_type(data.get("type"))
                account_type = resolve_account_type(login, stored_type)
                if account_type == "unknown":
                    account_type = stored_type

                if account_type != stored_type and account_type != "unknown":
                    save_account(login, account_type)

                return {"login": login, "type": account_type}
        except Exception as exc:
            logger.warning(f"Could not read account store {_STORE_PATH}: {exc}")

    try:
        current = int(os.getenv("MT5_CURRENT_ACCOUNT", "0") or 0)
    except ValueError:
        current = 0

    if current:
        account_type = resolve_account_type(current)
        if account_type != "unknown":
            return {"login": current, "type": account_type}

    accounts = _get_all_accounts()
    if accounts:
        try:
            login = int(accounts[0].get("login", 0))
        except (TypeError, ValueError):
            login = 0
        account_type = resolve_account_type(login, accounts[0].get("type"))
        return {"login": login, "type": account_type}

    return {"login": 0, "type": "unknown"}


def save_account(login: int, account_type: str) -> None:
    """Persist the active account to disk."""
    account_type = normalize_account_type(account_type)
    if account_type not in _VALID_ACCOUNT_TYPES:
        raise ValueError(f"Invalid account_type for save_account: {account_type!r}")

    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"login": int(login), "type": account_type}, indent=2)
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

    logger.info(f"Account saved: {login} ({account_type})")


def current_account_login() -> int:
    return int(load_account().get("login", 0) or 0)


def current_account_type() -> str:
    return normalize_account_type(load_account().get("type", "unknown"))


def is_demo() -> bool:
    return current_account_type() == "demo"


def current_mode() -> str:
    """Normalized runtime execution mode."""
    account_type = current_account_type()
    return account_type if account_type in _VALID_ACCOUNT_TYPES else "live"
