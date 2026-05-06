"""
Base Strategy — abstract contract every strategy must implement.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    direction: str          # "BUY", "SELL", or "NONE"
    entry_price: float
    sl_price: float
    tp_price: Optional[float]   # None = trailing stop only
    tp2_price: Optional[float] = None   # second partial target
    strategy: str = ""
    symbol: str = ""
    timeframe: str = ""
    confidence: float = 0.0
    comment: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("BUY", "SELL")


@dataclass
class StrategyResult:
    signal: Signal
    indicators: dict = field(default_factory=dict)  # debug snapshot


class BaseStrategy(ABC):

    name: str = "base"
    trading_type: str = "scalping"   # scalping | day_trading | swing
    timeframe: str = "M1"

    def __init__(self, symbol: str, params: dict):
        self.symbol = symbol
        self.params = params

    @abstractmethod
    def calculate(self, df: pd.DataFrame) -> StrategyResult:
        """
        Core strategy logic. Receives an OHLCV DataFrame (sorted oldest→newest).
        Returns a StrategyResult with a Signal and indicator snapshot.
        The last row is the most recent closed candle.
        """

    def _no_signal(self, indicators: Optional[dict] = None) -> StrategyResult:
        return StrategyResult(
            signal=Signal(
                direction="NONE",
                entry_price=0.0,
                sl_price=0.0,
                tp_price=None,
                strategy=self.name,
                symbol=self.symbol,
                timeframe=self.timeframe,
            ),
            indicators=indicators or {},
        )

    @staticmethod
    def _min_sl_dist(close: float) -> float:
        """
        Minimum SL distance that brokers will accept (50 points for scalping safety).
        Scales with price: covers 5-digit pairs (EUR/USD) and 3-digit (USD/JPY).
        Prevents "Invalid stops" rejections from tight scalping SLs.
        """
        # TODO:
        # Replace price-scaled fallback with symbol-aware MT5 stop-level logic
        # using symbol_info.trade_stops_level × point.
        # Current implementation is a generic safety fallback only.

        return close * 0.00050  # ~50 pts on 1.0 pairs, ~55 pts on 110 USDJPY

    def _sl_too_close(self, direction: str, entry: float, sl: float) -> bool:
        """Return True if the SL distance is below the broker minimum threshold."""
        return abs(entry - sl) < self._min_sl_dist(entry)
