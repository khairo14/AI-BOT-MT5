"""
Risk Manager — position sizing, SL/TP calculation, drawdown tracking,
circuit breaker, and trade eligibility checks.
Reads defaults from config/risk.json. All values are overridable at runtime.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"


def _load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


class RiskManager:
    """
    Calculates lot sizes, validates SL/TP placement, enforces
    drawdown limits, and manages the daily circuit breaker.
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()

        # Drawdown tracking (reset daily / weekly)
        self._day_start_balance: Optional[float] = None
        self._week_start_balance: Optional[float] = None
        self._tracking_date: Optional[date] = None
        self._tracking_week: Optional[int] = None

        # Consecutive loss tracking per trading mode
        self._consecutive_losses: dict[str, int] = {
            "scalping": 0,
            "day_trading": 0,
            "swing": 0,
        }
        self._paused_modes: dict[str, Optional[datetime]] = {
            "scalping": None,
            "day_trading": None,
            "swing": None,
        }

        # Global circuit breaker
        self._daily_halted = False
        self._weekly_halted = False

    # ------------------------------------------------------------------
    # Position Sizing
    # ------------------------------------------------------------------

    def calculate_lot_size(
        self,
        # ── MT5 symbol_info style (preferred) ───────────────────────────
        balance: Optional[float] = None,
        entry: Optional[float] = None,
        sl: Optional[float] = None,
        symbol: Optional[str] = None,
        contract_size: float = 100_000,
        tick_value: float = 1.0,    # account-currency value of 1 tick per 1 lot
        tick_size: float = 0.00001, # minimum price movement
        # ── legacy pip-based style (kept for back-compat) ────────────────
        account_balance: Optional[float] = None,
        entry_price: Optional[float] = None,
        sl_price: Optional[float] = None,
        pip_value: Optional[float] = None,
        pip_size: Optional[float] = None,
        # ── shared ───────────────────────────────────────────────────────
        risk_pct: Optional[float] = None,
        min_lot: float = 0.01,
        max_lot: float = 100.0,
        lot_step: float = 0.01,
    ) -> float:
        """
        Calculate lot size so that the SL distance risks exactly ``risk_pct``
        of the current account balance.

        Accepts two calling conventions:
            1. MT5 style: balance, entry, sl, tick_value, tick_size
            2. Legacy:    account_balance, entry_price, sl_price, pip_value, pip_size

        Formula (MT5 style):
            risk_amount  = balance × risk_pct / 100
            sl_ticks     = abs(entry - sl) / tick_size
            lot          = risk_amount / (sl_ticks × tick_value)
        """
        # Resolve params — MT5 style takes precedence
        _balance = balance if balance is not None else account_balance
        _entry   = entry   if entry   is not None else entry_price
        _sl      = sl      if sl      is not None else sl_price

        if _balance is None or _entry is None or _sl is None:
            logger.error("calculate_lot_size: missing balance/entry/sl — using min_lot")
            return min_lot

        risk_pct = risk_pct or self._config["risk_per_trade_pct"]
        max_risk_pct = self._config["max_risk_per_trade_pct"]
        risk_pct = min(risk_pct, max_risk_pct)

        risk_amount = _balance * (risk_pct / 100)

        # Determine value-per-tick
        if pip_value is not None and pip_size is not None:
            # Legacy path: treat pip as tick
            _tick_size  = pip_size
            _tick_value = pip_value
        else:
            _tick_size  = tick_size  if tick_size  > 0 else 0.00001
            _tick_value = tick_value if tick_value > 0 else 1.0

        sl_ticks = abs(_entry - _sl) / _tick_size

        if sl_ticks == 0:
            logger.error("SL distance is zero — cannot calculate lot size")
            return min_lot

        raw_lot = risk_amount / (sl_ticks * _tick_value)

        # Round down to nearest lot_step (never round up — avoids over-risking)
        lot = math.floor(raw_lot / lot_step) * lot_step
        lot = round(lot, 8)
        lot = max(min_lot, min(lot, max_lot))

        logger.debug(
            f"Lot size | Balance: {_balance} | Risk: {risk_pct}% "
            f"({risk_amount:.2f}) | SL ticks: {sl_ticks:.1f} | Lots: {lot}"
        )
        return lot

    # ------------------------------------------------------------------
    # SL / TP Validation
    # ------------------------------------------------------------------

    def validate_sl_tp(
        self,
        direction: str,
        entry_price: float,
        sl_price: Optional[float],
        tp_price: Optional[float],
    ) -> tuple[bool, str]:
        """
        Returns (is_valid, error_message).
        Checks: SL is required, SL is on correct side, R:R meets minimum.
        """
        if sl_price is None or sl_price == 0:
            return False, "SL is required on every trade"

        if direction == "BUY":
            if sl_price >= entry_price:
                return False, "BUY SL must be below entry price"
            sl_dist = entry_price - sl_price
        else:
            if sl_price <= entry_price:
                return False, "SELL SL must be above entry price"
            sl_dist = sl_price - entry_price

        if tp_price and tp_price != 0:
            if direction == "BUY":
                if tp_price <= entry_price:
                    return False, "BUY TP must be above entry price"
                tp_dist = tp_price - entry_price
            else:
                if tp_price >= entry_price:
                    return False, "SELL TP must be below entry price"
                tp_dist = entry_price - tp_price

            rr = tp_dist / sl_dist if sl_dist > 0 else 0
            min_rr = self._config["risk_reward_min"]
            if rr < min_rr - 1e-9:  # tolerance for floating-point precision
                return False, f"R:R {rr:.2f} is below minimum {min_rr}"

        return True, ""

    # ------------------------------------------------------------------
    # Drawdown & Circuit Breakers
    # ------------------------------------------------------------------

    def update_balance(self, current_balance: float) -> None:
        """
        Call this after every trade close. Tracks daily/weekly balance
        and triggers circuit breakers if thresholds are exceeded.
        """
        today = date.today()
        week_num = today.isocalendar().week

        # Reset daily tracking at start of new day
        if self._tracking_date != today:
            self._tracking_date = today
            self._day_start_balance = current_balance
            self._daily_halted = False
            logger.info(f"Daily balance reset: {current_balance}")

        # Reset weekly tracking at start of new week
        if self._tracking_week != week_num:
            self._tracking_week = week_num
            self._week_start_balance = current_balance
            self._weekly_halted = False
            logger.info(f"Weekly balance reset: {current_balance}")

        # Check daily drawdown
        if self._day_start_balance:
            daily_dd = (self._day_start_balance - current_balance) / self._day_start_balance * 100
            daily_limit = self._config["drawdown"]["daily_limit_pct"]
            if daily_dd >= daily_limit and not self._daily_halted:
                self._daily_halted = True
                logger.warning(
                    f"CIRCUIT BREAKER: Daily drawdown {daily_dd:.2f}% >= {daily_limit}%. "
                    "All new trades halted for today."
                )

        # Check weekly drawdown
        if self._week_start_balance:
            weekly_dd = (self._week_start_balance - current_balance) / self._week_start_balance * 100
            weekly_limit = self._config["drawdown"]["weekly_limit_pct"]
            if weekly_dd >= weekly_limit and not self._weekly_halted:
                self._weekly_halted = True
                logger.warning(
                    f"CIRCUIT BREAKER: Weekly drawdown {weekly_dd:.2f}% >= {weekly_limit}%. "
                    "All new trades halted until next Monday."
                )

    def record_loss(self, trading_mode: str) -> None:
        """Increment consecutive loss counter for a mode. Pauses mode if limit hit."""
        mode = trading_mode.lower()
        self._consecutive_losses[mode] = self._consecutive_losses.get(mode, 0) + 1
        limit = self._config["drawdown"]["max_consecutive_losses"]
        pause_hours = self._config["drawdown"]["consecutive_loss_pause_hours"]

        if self._consecutive_losses[mode] >= limit:
            self._paused_modes[mode] = datetime.now(tz=timezone.utc)
            logger.warning(
                f"Mode '{mode}' paused for {pause_hours}h after "
                f"{self._consecutive_losses[mode]} consecutive losses."
            )

    def record_win(self, trading_mode: str) -> None:
        """Reset consecutive loss counter on a win."""
        self._consecutive_losses[trading_mode.lower()] = 0

    def is_trading_allowed(self, trading_mode: str) -> tuple[bool, str]:
        """Returns (allowed, reason). Check before opening any new trade."""
        if self._daily_halted:
            return False, "Daily drawdown circuit breaker active — no new trades today"

        if self._weekly_halted:
            return False, "Weekly drawdown circuit breaker active — no new trades this week"

        mode = trading_mode.lower()
        pause_time = self._paused_modes.get(mode)
        if pause_time:
            pause_hours = self._config["drawdown"]["consecutive_loss_pause_hours"]
            elapsed = (datetime.now(tz=timezone.utc) - pause_time).total_seconds() / 3600
            if elapsed < pause_hours:
                remaining = pause_hours - elapsed
                return False, f"Mode '{mode}' paused — {remaining:.1f}h remaining"
            else:
                self._paused_modes[mode] = None
                self._consecutive_losses[mode] = 0
                logger.info(f"Mode '{mode}' pause lifted.")

        return True, ""

    # ------------------------------------------------------------------
    # Concurrent Trade Limit
    # ------------------------------------------------------------------

    def check_concurrent_limit(
        self,
        trading_mode: str,
        open_positions: list[dict],
    ) -> tuple[bool, str]:
        """
        Check if a new trade can be opened given current open position counts.
        `open_positions` should be the full list from MT5Client.get_open_positions().
        """
        mode = trading_mode.lower()
        limits = self._config["max_concurrent_trades"]
        mode_limit = limits.get(mode, 999)
        total_limit = limits.get("total", 999)

        # Count positions tagged with bot magic per mode
        # Mode is stored in the comment prefix: "scalp|", "day|", "swing|"
        prefix = {"scalping": "scalp", "day_trading": "day", "swing": "swing"}.get(mode, mode)
        mode_count = sum(
            1 for p in open_positions
            if p.get("comment", "").startswith(prefix)
        )
        total_count = len(open_positions)

        if mode_count >= mode_limit:
            return False, f"{trading_mode} limit reached ({mode_count}/{mode_limit})"
        if total_count >= total_limit:
            return False, f"Total position limit reached ({total_count}/{total_limit})"

        return True, ""

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """Re-read risk.json and apply updated thresholds at runtime."""
        try:
            self._config = self._load_config()
            logger.info("RiskManager: config reloaded from disk")
        except Exception as exc:
            logger.warning(f"RiskManager.reload_config failed: {exc}")

    def get_status(self) -> dict:
        return {
            "daily_halted":          self._daily_halted,
            "weekly_halted":         self._weekly_halted,
            "day_start_balance":     self._day_start_balance,
            "week_start_balance":    self._week_start_balance,
            "consecutive_losses":    self._consecutive_losses.copy(),
            "paused_modes":          {
                k: v.isoformat() if v else None
                for k, v in self._paused_modes.items()
            },
        }

    def reload_config(self) -> None:
        """Reload risk parameters from disk (useful after dashboard config changes)."""
        self._config = _load_config()
        logger.info("Risk config reloaded from disk.")
