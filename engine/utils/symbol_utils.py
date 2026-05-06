from __future__ import annotations

import re


def normalize_symbol(symbol: str) -> str:
    """
    Normalize broker-specific symbol variants.

    Examples:
        EURUSD#     -> EURUSD
        BTCUSDm     -> BTCUSD
        XAUUSD.pro  -> XAUUSD
    """
    if not symbol:
        return symbol

    s = symbol.upper().strip()

    # remove common suffixes
    s = re.sub(r"[#._\-].*$", "", s)

    # remove trailing broker letters
    s = re.sub(r"([A-Z]{6,10})[A-Z]+$", r"\1", s)

    return s