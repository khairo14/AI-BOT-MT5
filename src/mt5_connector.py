"""
mt5_connector.py – Manages the MetaTrader 5 connection lifecycle.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    mt5 = None  # type: ignore[assignment]
    MT5_AVAILABLE = False

from src.logger import get_logger

logger = get_logger("mt5_connector")


class MT5Connector:
    """Handles initialisation, login, and shutdown of the MT5 terminal connection."""

    def __init__(
        self,
        login: int,
        password: str,
        server: str,
        timeout: int = 60_000,
        portable: bool = False,
    ) -> None:
        self.login = login
        self.password = password
        self.server = server
        self.timeout = timeout
        self.portable = portable
        self._connected: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Initialise the MT5 terminal and log in to the broker account.

        Returns:
            True if the connection and login succeed, False otherwise.
        """
        if not MT5_AVAILABLE:
            logger.error("MetaTrader5 package is not installed or not supported on this OS.")
            return False

        if not mt5.initialize(timeout=self.timeout, portable=self.portable):  # type: ignore[union-attr]
            logger.error("MT5 initialise failed: %s", mt5.last_error())  # type: ignore[union-attr]
            return False

        # Override credentials with environment variables when set
        login = int(os.getenv("MT5_LOGIN", str(self.login)))
        password = os.getenv("MT5_PASSWORD", self.password)
        server = os.getenv("MT5_SERVER", self.server)

        if not mt5.login(login, password=password, server=server):  # type: ignore[union-attr]
            logger.error(
                "MT5 login failed for account %s on server %s: %s",
                login,
                server,
                mt5.last_error(),  # type: ignore[union-attr]
            )
            mt5.shutdown()  # type: ignore[union-attr]
            return False

        self._connected = True
        info = mt5.account_info()  # type: ignore[union-attr]
        logger.info(
            "Connected to MT5 | Server: %s | Account: %s | Balance: %.2f %s",
            server,
            login,
            info.balance if info else 0.0,
            info.currency if info else "",
        )
        return True

    def disconnect(self) -> None:
        """Shut down the MT5 terminal connection."""
        if MT5_AVAILABLE and self._connected:
            mt5.shutdown()  # type: ignore[union-attr]
            self._connected = False
            logger.info("MT5 connection closed.")

    def is_connected(self) -> bool:
        """Return True if the terminal is currently connected."""
        return self._connected

    def get_account_info(self) -> Optional[object]:
        """Return the MT5 AccountInfo named tuple, or None on failure."""
        if not self._connected or not MT5_AVAILABLE:
            return None
        return mt5.account_info()  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "MT5Connector":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()
