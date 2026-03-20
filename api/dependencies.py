"""
Shared FastAPI dependencies — MT5 client injection for all route handlers.
"""

import json
from pathlib import Path

from fastapi import Header, HTTPException
from engine.mt5_client import MT5Client

_APP_CFG_PATH = Path(__file__).parent.parent / "config" / "app.json"


def verify_api_key(x_api_key: str = Header(default="")) -> None:
    """
    Optional API-key guard. If 'api_key' is set in config/app.json the
    request must supply a matching X-Api-Key header; if the key is absent
    or empty in config, all requests are allowed through.
    """
    try:
        cfg = json.loads(_APP_CFG_PATH.read_text(encoding="utf-8"))
        expected = cfg.get("api_key", "")
        if expected and x_api_key != expected:
            raise HTTPException(status_code=401, detail="Invalid or missing API key")
    except HTTPException:
        raise
    except Exception:
        pass  # config unreadable — fail open so the bot stays operational


def get_client() -> MT5Client:
    """
    Dependency that returns the app-level MT5 client.
    Raises 503 if MT5 is not connected.
    """
    from api.main import get_mt5_client
    client = get_mt5_client()
    if client is None or not client.is_connected():
        raise HTTPException(status_code=503, detail="MT5 is not connected")
    return client
