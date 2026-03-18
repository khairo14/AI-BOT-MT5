"""
risk_manager.py – Enforces position-sizing and daily-loss limits.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from src.logger import get_logger

logger = get_logger("risk_manager")


class RiskManager:
    """
    Calculates lot sizes, validates new trade entries, and tracks daily loss.
    """

    def __init__(
        self,
        risk_per_trade: float = 0.01,
        max_trades: int = 3,
        stop_loss_pips: int = 30,
        take_profit_pips: int = 60,
        max_daily_loss: float = 0.05,
        trailing_stop: bool = False,
        trailing_stop_pips: int = 15,
    ) -> None:
        if not 0 < risk_per_trade < 1:
            raise ValueError("risk_per_trade must be between 0 and 1 exclusive.")
        if max_trades < 1:
            raise ValueError("max_trades must be at least 1.")

        self.risk_per_trade = risk_per_trade
        self.max_trades = max_trades
        self.stop_loss_pips = stop_loss_pips
        self.take_profit_pips = take_profit_pips
        self.max_daily_loss = max_daily_loss
        self.trailing_stop = trailing_stop
        self.trailing_stop_pips = trailing_stop_pips

        self._daily_loss: float = 0.0
        self._daily_loss_date: date = date.today()
        self._open_trades: int = 0

    # ------------------------------------------------------------------
    # Daily loss tracking
    # ------------------------------------------------------------------

    def _reset_daily_loss_if_new_day(self) -> None:
        today = date.today()
        if today != self._daily_loss_date:
            self._daily_loss = 0.0
            self._daily_loss_date = today
            logger.debug("Daily loss counter reset for %s.", today)

    def record_trade_result(self, profit: float) -> None:
        """Update the cumulative daily loss with a closed-trade profit/loss.

        Args:
            profit: Positive for profit, negative for loss (account currency).
        """
        self._reset_daily_loss_if_new_day()
        if profit < 0:
            self._daily_loss += abs(profit)
        self._open_trades = max(0, self._open_trades - 1)

    def on_trade_opened(self) -> None:
        """Call when a trade is successfully opened."""
        self._open_trades += 1

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def can_open_trade(self, account_balance: float) -> bool:
        """Return True if opening a new trade is allowed given current risk limits.

        Args:
            account_balance: Current account balance in account currency.
        """
        self._reset_daily_loss_if_new_day()

        if self._open_trades >= self.max_trades:
            logger.info(
                "Max open trades reached (%d/%d). Trade blocked.",
                self._open_trades,
                self.max_trades,
            )
            return False

        daily_loss_pct = self._daily_loss / account_balance if account_balance > 0 else 0.0
        if daily_loss_pct >= self.max_daily_loss:
            logger.info(
                "Daily loss limit reached (%.2f%% >= %.2f%%). Trading halted for today.",
                daily_loss_pct * 100,
                self.max_daily_loss * 100,
            )
            return False

        return True

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def calculate_lot_size(
        self,
        account_balance: float,
        pip_value: float,
        symbol_info: Optional[object] = None,
    ) -> float:
        """Calculate the position size (lots) based on fixed fractional risk.

        The formula is:
            lot_size = (balance * risk_per_trade) / (stop_loss_pips * pip_value_per_lot)

        Args:
            account_balance: Account balance in account currency.
            pip_value: Value of one pip per standard lot (1.0 for most symbols; 10 USD
                       for EURUSD on a USD account).
            symbol_info: Optional MT5 SymbolInfo object used to clamp the lot size
                         to [volume_min, volume_max] at volume_step intervals.

        Returns:
            Lot size rounded to the symbol's volume step, clamped to valid range.
        """
        risk_amount = account_balance * self.risk_per_trade
        raw_lots = risk_amount / (self.stop_loss_pips * pip_value)

        if symbol_info is not None:
            vol_min = getattr(symbol_info, "volume_min", 0.01)
            vol_max = getattr(symbol_info, "volume_max", 100.0)
            vol_step = getattr(symbol_info, "volume_step", 0.01)
            # Quantise to volume step
            raw_lots = round(raw_lots / vol_step) * vol_step
            raw_lots = max(vol_min, min(vol_max, raw_lots))
        else:
            raw_lots = max(0.01, round(raw_lots, 2))

        logger.debug(
            "Lot size calculated: %.2f (balance=%.2f, risk=%.1f%%, SL=%d pips, pip_value=%.4f)",
            raw_lots,
            account_balance,
            self.risk_per_trade * 100,
            self.stop_loss_pips,
            pip_value,
        )
        return raw_lots

    def calculate_sl_tp(
        self, entry_price: float, signal: int, pip_size: float = 0.0001
    ) -> tuple[float, float]:
        """Calculate absolute stop-loss and take-profit prices.

        Args:
            entry_price: Order entry price.
            signal: SIGNAL_BUY (1) or SIGNAL_SELL (-1).
            pip_size: Size of one pip for the symbol (e.g. 0.0001 for EURUSD,
                      0.01 for USDJPY).

        Returns:
            (stop_loss_price, take_profit_price)
        """
        sl_distance = self.stop_loss_pips * pip_size
        tp_distance = self.take_profit_pips * pip_size

        if signal == 1:  # BUY
            sl = entry_price - sl_distance
            tp = entry_price + tp_distance
        else:  # SELL
            sl = entry_price + sl_distance
            tp = entry_price - tp_distance

        return round(sl, 5), round(tp, 5)
