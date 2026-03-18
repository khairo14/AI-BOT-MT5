"""
Shared FastAPI dependencies — MT5 client injection for all route handlers.
"""

from fastapi import HTTPException
from engine.mt5_client import MT5Client


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
