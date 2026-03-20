"""
Account store — persists the active trading mode (paper/live) to disk.

config/account_mode.json is written every time the mode is changed via the API
or start-up, so restarts always resume with the last chosen mode.

The .env file is the authoritative source at first boot; account_mode.json
takes precedence on subsequent starts so that dashboard-initiated mode switches
survive a restart without needing to edit .env.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from typing import Literal, Optional

from loguru import logger

AccountMode = Literal["paper", "live"]

_STORE_PATH = Path(__file__).parent.parent / "config" / "account_mode.json"
_VALID_MODES = frozenset({"paper", "live"})


def load_mode() -> AccountMode:
    """
    Return the active trading mode.

    Priority:
      1. config/account_mode.json (set by last dashboard switch)
      2. TRADING_MODE env var
      3. "paper" (safe default)
    """
    if _STORE_PATH.exists():
        try:
            data = json.loads(_STORE_PATH.read_text())
            mode = data.get("mode", "").lower()
            if mode in _VALID_MODES:
                return mode  # type: ignore[return-value]
        except Exception:
            pass

    env_mode = os.getenv("TRADING_MODE", "paper").lower()
    return env_mode if env_mode in _VALID_MODES else "paper"  # type: ignore[return-value]


def save_mode(mode: str) -> None:
    """Persist the active mode to disk. Raises ValueError on invalid input."""
    mode = mode.lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"Invalid trading mode: {mode!r}. Must be 'paper' or 'live'.")
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"mode": mode}, indent=2)
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
    os.environ["TRADING_MODE"] = mode
    logger.info(f"Trading mode saved: {mode.upper()}")


def current_mode() -> AccountMode:
    """Convenience alias for load_mode()."""
    return load_mode()
