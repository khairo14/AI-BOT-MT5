"""
Risk Manager — position sizing, SL/TP calculation, drawdown tracking,
circuit breaker, and trade eligibility checks.
Reads defaults from config/risk.json. All values are overridable at runtime.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, Literal

from loguru import logger
from engine.account_store import current_account_login, current_mode

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"
_STATE_PATH = Path(__file__).parent.parent / "data" / "risk_state.json"
_PRESETS_PATH = Path(__file__).parent.parent / "config" / "risk_presets.json"

CREDIT_RISK_UTILIZATION = 0.75

def effective_risk_capital(balance: float, credit: float = 0.0) -> float:
    """
    Risk capital used for sizing/audit.

    Uses full balance plus 75% of broker credit/bonus.
    This lets EVOTRADE utilize allowed broker credit without treating it as
    fully-owned capital.
    """
    balance = max(float(balance or 0.0), 0.0)
    credit = max(float(credit or 0.0), 0.0)
    return balance + (credit * CREDIT_RISK_UTILIZATION)

def _load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def _load_presets() -> dict:
    try:
        with open(_PRESETS_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"Risk presets not found at {_PRESETS_PATH}, using defaults")
        return {"current_preset": "moderate", "presets": {}}

def _notify_risk_alert(
    *,
    title: str,
    message: str,
    severity: Literal["info", "success", "warning", "error"] = "warning",
    metadata: dict | None = None,
) -> None:
    try:
        from engine.notification_manager import notification_manager

        notification_manager.add(
            type="circuit_breaker",
            title=title,
            message=message,
            severity=severity,
            metadata=metadata or {},
        )
    except Exception:
        pass

class RiskManager:
    """
    Calculates lot sizes, validates SL/TP placement, enforces
    drawdown limits, and manages the daily circuit breaker.
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._presets_data = _load_presets()
        self._current_preset = self._presets_data.get("current_preset", "moderate")

        # Drawdown tracking (reset daily / weekly)
        self._day_start_balance: Optional[float] = None
        self._week_start_balance: Optional[float] = None
        self._tracking_date: Optional[date] = None
        self._tracking_week: Optional[tuple[int, int]] = None

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

        # Per-strategy consecutive loss tracking (keyed by strategy_name)
        self._strategy_losses: dict[str, int] = {}
        self._strategy_paused: dict[str, Optional[datetime]] = {}

        # Global circuit breaker
        self._daily_halted = False
        self._weekly_halted = False
        self._cb_enabled = True
        self._on_circuit_breaker = None

        # RISK-3: timestamp of last mode-switch
        self._mode_switch_ts: Optional[datetime] = None

        # Lock protecting all mutable state
        self._lock = threading.Lock()

        if config is None:
            self._load_state()

    def _state_path(self) -> Path:
        """
        Per-account risk state isolation.
        Prevents demo/live or multiple MT5 accounts from sharing
        drawdown/circuit-breaker state.
        """
        try:
            login = str(current_account_login() or "unknown")
        except Exception:
            login = "unknown"

        try:
            mode = str(current_mode() or "unknown").lower().strip()
            if mode in ("paper", "demo", "test"):
                mode = "demo"
            elif mode != "live":
                mode = "unknown"
        except Exception:
            mode = "unknown"

        return (
            _STATE_PATH.parent /
            f"risk_state_{mode}_{login}.json"
        )
    # ------------------------------------------------------------------
    # State Persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist circuit breaker state to disk (called inside self._lock)."""
        try:
            state = {
                "daily_halted":        self._daily_halted,
                "weekly_halted":       self._weekly_halted,
                "day_start_balance":   self._day_start_balance,
                "week_start_balance":  self._week_start_balance,
                "tracking_date":       self._tracking_date.isoformat() if self._tracking_date else None,
                "tracking_week":       list(self._tracking_week) if self._tracking_week else None,
                "consecutive_losses":  self._consecutive_losses,
                "paused_modes":        {
                    k: v.isoformat() if v else None
                    for k, v in self._paused_modes.items()
                },
                "strategy_losses":     self._strategy_losses,
                "strategy_paused":     {
                    k: v.isoformat() if v else None
                    for k, v in self._strategy_paused.items()
                },
            }
            state_path = self._state_path()
            state_path.parent.mkdir(parents=True, exist_ok=True)
            _serialized = json.dumps(state)
            fd, _tmp = tempfile.mkstemp(dir=str(state_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as _f:
                    _f.write(_serialized)
                os.replace(_tmp, state_path)
            except Exception:
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:
            logger.warning(f"RiskManager: could not save state: {exc}")

    def _load_state(self) -> None:
        """Restore circuit breaker state from disk if available."""
        if not self._state_path().exists():
            return
        try:
            state = json.loads(self._state_path().read_text(encoding="utf-8"))
            self._daily_halted       = bool(state.get("daily_halted", False))
            self._weekly_halted      = bool(state.get("weekly_halted", False))
            self._day_start_balance  = state.get("day_start_balance")
            self._week_start_balance = state.get("week_start_balance")
            self._tracking_date      = date.fromisoformat(state["tracking_date"]) if state.get("tracking_date") else None
            _tw = state.get("tracking_week")
            if isinstance(_tw, list) and len(_tw) == 2:
                self._tracking_week = (int(_tw[0]), int(_tw[1]))
            elif isinstance(_tw, tuple) and len(_tw) == 2:
                self._tracking_week = (int(_tw[0]), int(_tw[1]))
            else:
                self._tracking_week = None
            
            for k, v in state.get("consecutive_losses", {}).items():
                if k in self._consecutive_losses:
                    self._consecutive_losses[k] = int(v)
            for k, v in state.get("paused_modes", {}).items():
                if k in self._paused_modes:
                    self._paused_modes[k] = datetime.fromisoformat(v) if v else None
            for k, v in state.get("strategy_losses", {}).items():
                self._strategy_losses[k] = int(v)
            for k, v in state.get("strategy_paused", {}).items():
                self._strategy_paused[k] = datetime.fromisoformat(v) if v else None
            logger.info("RiskManager: circuit breaker state restored from disk")
        except Exception as exc:
            logger.warning(f"RiskManager: could not restore state: {exc}")

    # ------------------------------------------------------------------
    # Position Sizing
    # ------------------------------------------------------------------

    def _get_safe_tick_value(self, tick_value: float, symbol: str, tick_size: float) -> float:
        """
        Ensure tick_value is not unrealistically low for the instrument type.
        MT5 sometimes reports tick_value = $0.01 for futures when it should be $1.00,
        causing lot sizes to be 100x too large.
        """
        if tick_value >= 0.50:
            return tick_value
        
        _sym_u = symbol.upper() if symbol else ""
        
        # Penny stocks and micro instruments can have very low tick values
        if any(x in _sym_u for x in ("PENNY", "CENTS", "MICRO")):
            return tick_value  # Keep as-is, these are legitimate low tick values
        
        # For all normal instruments (forex, indices, commodities, crypto),
        # tick value below $0.50 is suspicious
        logger.warning(
            f"Tick value {tick_value:.4f} for {symbol} is suspiciously low. "
            f"Clamping to $1.00 to prevent over-sizing. "
            f"SL ticks will be adjusted accordingly."
        )
        return 1.0

    def calculate_lot_size(
        self,
        # ── MT5 symbol_info style (preferred) ───────────────────────────
        balance: Optional[float] = None,
        credit: float = 0.0,
        entry: Optional[float] = None,
        sl: Optional[float] = None,
        symbol: Optional[str] = None,
        tick_value: float = 1.0,
        tick_size: float = 0.00001,
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
            logger.error("calculate_lot_size: missing balance/entry/sl — rejecting trade")
            return 0.0

        risk_pct = risk_pct or self._config["risk_per_trade_pct"]
        max_risk_pct = self._config["max_risk_per_trade_pct"]
        risk_pct = min(risk_pct, max_risk_pct)

        _effective_balance = effective_risk_capital(float(_balance), credit)
        risk_amount = _effective_balance * (risk_pct / 100)

        # Determine value-per-tick
        if pip_value is not None and pip_size is not None:
            # Legacy path: treat pip as tick
            _tick_size  = pip_size
            _tick_value = pip_value
        else:
            _tick_size  = tick_size  if tick_size  > 0 else 0.00001
            _tick_value = tick_value if tick_value > 0 else 1.0

        # Apply safety clamp to tick_value for suspiciously low values
        _tick_value = self._get_safe_tick_value(_tick_value, symbol or "", _tick_size)

        sl_ticks = abs(_entry - _sl) / _tick_size

        if sl_ticks == 0:
            logger.error("calculate_lot_size: SL distance is zero — signal must be rejected upstream")
            return 0.0

        raw_lot = risk_amount / (sl_ticks * _tick_value)
        
        # SAFETY GUARD: validate that the calculated lot doesn't exceed intended risk.
        # Recompute using a floor on tick_value as a cross-check.
        _tick_value_safe = max(_tick_value, 0.50)
        _safe_lot = risk_amount / (sl_ticks * _tick_value_safe) if sl_ticks > 0 else raw_lot
        if _safe_lot < raw_lot:
            logger.warning(
                f"Lot safety clamp: tick_value={_tick_value:.4f} seems low — "
                f"using floor 0.50. Lot reduced from {raw_lot:.5f} to {_safe_lot:.5f} "
                f"(risk_amount={risk_amount:.2f})"
            )
            raw_lot = _safe_lot

        # Round down to nearest lot_step (never round up — avoids over-risking)
        lot = math.floor(raw_lot / lot_step) * lot_step
        lot = round(lot, 8)

        # Detect when broker minimum lot would exceed intended risk.
        if raw_lot > 0 and lot < min_lot:
            actual_risk_pct = (min_lot / raw_lot) * risk_pct
            logger.warning(
                f"Lot size {raw_lot:.5f} is below broker minimum {min_lot}. "
                f"Minimum lot would risk {actual_risk_pct:.2f}% "
                f"(target {risk_pct:.2f}%). Rejecting trade instead of forcing min_lot."
            )

        if lot < min_lot:
            logger.warning(
                f"Calculated lot {lot:.8f} is below broker minimum {min_lot}. "
                "Rejecting trade instead of forcing min_lot to avoid over-risk."
            )
            return 0.0

        lot = min(lot, max_lot)

        logger.debug(
            f"Lot size | Balance: {_balance} | Credit: {credit} | "
            f"Effective risk capital: {_effective_balance:.2f} | Risk: {risk_pct}% "
            f"({risk_amount:.2f}) | SL ticks: {sl_ticks:.1f} | Lots: {lot}"
        )
        return lot

    def adjust_lot_for_volatility(
        self,
        lot: float,
        trading_type: str,
        atr_pct: float,
        min_lot: float = 0.01,
        lot_step: float = 0.01,
    ) -> float:
        """
        Scale down lot size when current ATR% exceeds the baseline for this
        trading type. Protects against oversizing during high-volatility regimes.
        """
        _BASELINE_ATR: dict[str, float] = {
            "scalping":    0.15,
            "day_trading": 0.50,
            "swing":       1.50,
        }
        baseline = _BASELINE_ATR.get(trading_type, 0.50)
        if baseline <= 0 or atr_pct <= baseline:
            return lot

        ratio = atr_pct / baseline
        if ratio <= 1.0:
            factor = 1.0
        elif ratio <= 2.0:
            factor = 1.0 - (ratio - 1.0) * 0.25
        elif ratio <= 3.0:
            factor = 0.75 - (ratio - 2.0) * 0.15
        else:
            factor = 0.50

        adjusted = math.floor(lot * factor / lot_step) * lot_step
        adjusted = round(max(min_lot, adjusted), 8)

        if adjusted < lot:
            logger.info(
                f"Volatility adjustment [{trading_type}]: "
                f"ATR%={atr_pct:.2f} ({ratio:.1f}× baseline) → "
                f"lot {lot} → {adjusted} (factor={factor:.2f})"
            )
        return adjusted

    # ------------------------------------------------------------------
    # SL / TP Validation
    # ------------------------------------------------------------------

    def validate_sl_tp(
        self,
        direction: str,
        entry_price: float,
        sl_price: Optional[float],
        tp_price: Optional[float],
        trading_type: Optional[str] = None,
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
            _by_mode = self._config.get("risk_reward_min_by_mode", {})
            min_rr = _by_mode.get(trading_type, self._config["risk_reward_min"]) if trading_type else self._config["risk_reward_min"]
            if rr < min_rr - 1e-9:
                return False, f"R:R {rr:.2f} is below minimum {min_rr} for {trading_type or 'trade'}"

        return True, ""

    # ------------------------------------------------------------------
    # Drawdown & Circuit Breakers
    # ------------------------------------------------------------------

    def update_balance(self, current_balance: float) -> None:
        """Call after every trade close. Tracks daily/weekly balance and triggers circuit breakers."""
        with self._lock:
            _now_utc  = datetime.now(tz=timezone.utc)
            today     = _now_utc.date()
            _iso      = _now_utc.isocalendar()
            week_key  = (_iso.year, _iso.week)

            if self._tracking_date != today:
                self._tracking_date = today
                self._day_start_balance = current_balance
                self._daily_halted = False
                logger.info(f"Daily balance reset: {current_balance}")

            if self._tracking_week != week_key:
                self._tracking_week = week_key
                self._week_start_balance = current_balance
                self._weekly_halted = False
                logger.info(f"Weekly balance reset: {current_balance}")

            if self._day_start_balance:
                daily_dd = (self._day_start_balance - current_balance) / self._day_start_balance * 100
                daily_limit = self._config["drawdown"]["daily_limit_pct"]
                if self._cb_enabled and daily_dd >= daily_limit and not self._daily_halted:
                    self._daily_halted = True
                    logger.warning(
                        f"CIRCUIT BREAKER: Daily drawdown {daily_dd:.2f}% >= {daily_limit}%. "
                        "All new trades halted for today."
                    )
                    _notify_risk_alert(
                        title="Daily Circuit Breaker",
                        message=(
                            f"Daily drawdown reached {daily_dd:.2f}% "
                            f"(limit {daily_limit:.2f}%). New trades halted for today."
                        ),
                        severity="error",
                        metadata={
                            "type": "daily_drawdown",
                            "drawdown_pct": round(daily_dd, 2),
                            "limit_pct": daily_limit,
                        },
                    )
                    if self._on_circuit_breaker:
                        self._on_circuit_breaker("daily", f"Daily drawdown {daily_dd:.2f}% reached {daily_limit}% limit")

            if self._week_start_balance:
                weekly_dd = (self._week_start_balance - current_balance) / self._week_start_balance * 100
                weekly_limit = self._config["drawdown"]["weekly_limit_pct"]
                if self._cb_enabled and weekly_dd >= weekly_limit and not self._weekly_halted:
                    self._weekly_halted = True
                    logger.warning(
                        f"CIRCUIT BREAKER: Weekly drawdown {weekly_dd:.2f}% >= {weekly_limit}%. "
                        "All new trades halted until next Monday."
                    )
                    _notify_risk_alert(
                        title="Weekly Circuit Breaker",
                        message=(
                            f"Weekly drawdown reached {weekly_dd:.2f}% "
                            f"(limit {weekly_limit:.2f}%). New trades halted until next week."
                        ),
                        severity="error",
                        metadata={
                            "type": "weekly_drawdown",
                            "drawdown_pct": round(weekly_dd, 2),
                            "limit_pct": weekly_limit,
                        },
                    )
                    if self._on_circuit_breaker:
                        self._on_circuit_breaker("weekly", f"Weekly drawdown {weekly_dd:.2f}% reached {weekly_limit}% limit")
            self._save_state()

    def record_loss(self, trading_mode: str, strategy_name: str | None = None) -> None:
        """Increment consecutive loss counter for a mode (and optionally a strategy)."""
        mode = trading_mode.lower()
        with self._lock:
            self._consecutive_losses[mode] = self._consecutive_losses.get(mode, 0) + 1
            if not self._cb_enabled:
                return
            limit = self._config["drawdown"]["max_consecutive_losses"]
            pause_hours = self._config["drawdown"]["consecutive_loss_pause_hours"]
            if self._consecutive_losses[mode] >= limit:
                self._paused_modes[mode] = datetime.now(tz=timezone.utc)
                logger.warning(
                    f"Mode '{mode}' paused for {pause_hours}h after "
                    f"{self._consecutive_losses[mode]} consecutive losses."
                )
                _notify_risk_alert(
                    title="Mode Paused",
                    message=(
                        f"{mode} paused for {pause_hours}h after "
                        f"{self._consecutive_losses[mode]} consecutive losses."
                    ),
                    severity="warning",
                    metadata={
                        "type": "mode_consecutive_losses",
                        "mode": mode,
                        "losses": self._consecutive_losses[mode],
                        "pause_hours": pause_hours,
                    },
                )
            if strategy_name:
                self._strategy_losses[strategy_name] = self._strategy_losses.get(strategy_name, 0) + 1
                strat_limit = self._config["drawdown"].get("max_strategy_consecutive_losses", limit)
                strat_pause_hours = self._config["drawdown"].get("strategy_pause_hours", pause_hours)
                if self._strategy_losses[strategy_name] >= strat_limit:
                    self._strategy_paused[strategy_name] = datetime.now(tz=timezone.utc)
                    logger.warning(
                        f"Strategy '{strategy_name}' paused for {strat_pause_hours}h after "
                        f"{self._strategy_losses[strategy_name]} consecutive losses."
                    )
                    _notify_risk_alert(
                        title="Strategy Paused",
                        message=(
                            f"{strategy_name} paused for {strat_pause_hours}h after "
                            f"{self._strategy_losses[strategy_name]} consecutive losses."
                        ),
                        severity="warning",
                        metadata={
                            "type": "strategy_consecutive_losses",
                            "strategy": strategy_name,
                            "losses": self._strategy_losses[strategy_name],
                            "pause_hours": strat_pause_hours,
                        },
                    )
            self._save_state()

    def record_win(self, trading_mode: str, strategy_name: str | None = None) -> None:
        """Reset consecutive loss counter on a win."""
        with self._lock:
            self._consecutive_losses[trading_mode.lower()] = 0
            if strategy_name:
                self._strategy_losses[strategy_name] = 0
                self._strategy_paused.pop(strategy_name, None)
            self._save_state()

    def is_strategy_allowed(self, strategy_name: str) -> tuple[bool, str]:
        """Check if a specific strategy is paused by its consecutive-loss circuit breaker."""
        with self._lock:
            paused_at = self._strategy_paused.get(strategy_name)
            if paused_at is None:
                return True, ""
            pause_hours = self._config["drawdown"].get(
                "strategy_pause_hours",
                self._config["drawdown"]["consecutive_loss_pause_hours"],
            )
            elapsed = (datetime.now(tz=timezone.utc) - paused_at).total_seconds() / 3600
            if elapsed >= pause_hours:
                self._strategy_paused.pop(strategy_name, None)
                self._strategy_losses[strategy_name] = 0
                return True, ""
            remaining = pause_hours - elapsed
            return False, (
                f"Strategy '{strategy_name}' paused for {remaining:.1f}h after "
                f"{self._strategy_losses.get(strategy_name, 0)} consecutive losses."
            )

    def strategy_status(self) -> dict:
        """Return per-strategy loss counters and pause state."""
        with self._lock:
            pause_hours = self._config["drawdown"].get(
                "strategy_pause_hours",
                self._config["drawdown"]["consecutive_loss_pause_hours"],
            )
            strat_limit = self._config["drawdown"].get(
                "max_strategy_consecutive_losses",
                self._config["drawdown"]["max_consecutive_losses"],
            )
            result = {}
            for name, losses in self._strategy_losses.items():
                paused_at = self._strategy_paused.get(name)
                halted = False
                remaining_h = 0.0
                if paused_at is not None:
                    elapsed = (datetime.now(tz=timezone.utc) - paused_at).total_seconds() / 3600
                    if elapsed < pause_hours:
                        halted = True
                        remaining_h = round(pause_hours - elapsed, 1)
                result[name] = {
                    "consecutive_losses": losses,
                    "limit": strat_limit,
                    "halted": halted,
                    "remaining_hours": remaining_h,
                }
            return result

    def reset_drawdown(self, current_balance: Optional[float] = None) -> None:
        """Manually clear daily/weekly circuit-breaker halts."""
        with self._lock:
            self._daily_halted = False
            self._weekly_halted = False
            if current_balance is not None:
                self._day_start_balance = current_balance
                self._week_start_balance = current_balance
            self._save_state()
        logger.info("Circuit breaker: drawdown halts manually cleared.")

    def reset_consecutive_losses(self) -> None:
        """Clear all consecutive-loss counters and mode pauses."""
        with self._lock:
            for mode in self._consecutive_losses:
                self._consecutive_losses[mode] = 0
                self._paused_modes[mode] = None
            self._strategy_losses.clear()
            self._strategy_paused.clear()
            self._save_state()
        logger.info("Circuit breaker: consecutive-loss counters reset.")

    def reset_for_mode_switch(self) -> None:
        """Reset all state when switching between paper and live modes."""
        with self._lock:
            self._daily_halted = False
            self._weekly_halted = False
            self._day_start_balance = None
            self._week_start_balance = None
            self._tracking_date = None
            self._tracking_week = None
            for mode in self._consecutive_losses:
                self._consecutive_losses[mode] = 0
                self._paused_modes[mode] = None
            self._strategy_losses.clear()
            self._strategy_paused.clear()
            self._mode_switch_ts = datetime.now(tz=timezone.utc)
            self._save_state()
        logger.info("RiskManager: all state reset for mode switch.")

    def set_circuit_breaker_enabled(self, enabled: bool) -> None:
        """Enable or disable the circuit breaker globally."""
        self._cb_enabled = enabled
        logger.info(f"Circuit breaker {'enabled' if enabled else 'DISABLED'} via API.")

    def is_trading_allowed(self, trading_mode: str) -> tuple[bool, str]:
        """Check if trading is allowed for a mode (circuit breakers, pauses)."""
        with self._lock:
            if not self._cb_enabled:
                return True, ""

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
        symbol: str | None = None,
    ) -> tuple[bool, str]:
        """Check if a new trade would exceed per-mode or per-symbol concurrent limits."""
        with self._lock:
            if self._mode_switch_ts is not None:
                elapsed = (datetime.now(tz=timezone.utc) - self._mode_switch_ts).total_seconds()
                if elapsed < 1.0:
                    return False, "Mode switch settling — retry in a moment"
            
            mode = trading_mode.lower()
            limits = self._config["max_concurrent_trades"]
            mode_limit = limits.get(mode, 999)
            total_limit = limits.get("total", 999)
            per_symbol_limit = limits.get("per_symbol", 999)

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

            if symbol is not None:
                symbol_count = sum(
                    1 for p in open_positions
                    if p.get("symbol") == symbol and p.get("comment", "").startswith(prefix)
                )
                if symbol_count >= per_symbol_limit:
                    return False, f"Per-symbol limit reached for {symbol} ({symbol_count}/{per_symbol_limit})"

            return True, ""

    # ------------------------------------------------------------------
    # Status & Config
    # ------------------------------------------------------------------

    def reload_config(self) -> None:
        """Re-read risk.json and apply updated thresholds at runtime."""
        try:
            self._config = _load_config()
            logger.info("RiskManager: config reloaded from disk")
        except Exception as exc:
            logger.warning(f"RiskManager.reload_config failed: {exc}")

    def get_status(self) -> dict:
        return {
            "circuit_breaker_enabled": self._cb_enabled,
            "daily_halted":          self._daily_halted,
            "weekly_halted":         self._weekly_halted,
            "day_start_balance":     self._day_start_balance,
            "week_start_balance":    self._week_start_balance,
            "consecutive_losses":    self._consecutive_losses.copy(),
            "paused_modes":          {
                k: v.isoformat() if v else None
                for k, v in self._paused_modes.items()
            },
            "current_risk_preset": self._current_preset,
        }

    # ------------------------------------------------------------------
    # Risk Presets
    # ------------------------------------------------------------------

    def get_available_presets(self) -> dict:
        """Return all available presets with their configurations."""
        return {
            "current": self._current_preset,
            "presets": self._presets_data.get("presets", {})
        }

    def set_risk_preset(self, preset_name: str) -> tuple[bool, str]:
        """Set the active risk preset by writing its values directly to risk.json."""
        presets = self._presets_data.get("presets", {})
        if preset_name not in presets:
            available = ", ".join(presets.keys())
            return False, f"Unknown preset '{preset_name}'. Available: {available}"

        preset = presets[preset_name]
        
        self._config["risk_per_trade_pct"] = preset.get("risk_per_trade_pct", 1.5)
        self._config["max_risk_per_trade_pct"] = preset.get("max_risk_per_trade_pct", 2.0)
        self._config["max_concurrent_trades"] = preset.get("max_concurrent_trades", {})
        self._config["drawdown"] = preset.get("drawdown", {})
        
        try:
            _serialized = json.dumps(self._config, indent=2)
            _fd, _tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
            try:
                with os.fdopen(_fd, "w", encoding="utf-8") as _tf:
                    _tf.write(_serialized)
                os.replace(_tmp, CONFIG_PATH)
            except Exception:
                try:
                    os.unlink(_tmp)
                except OSError:
                    pass
                raise
            logger.info(f"Risk config updated from '{preset_name}' preset")
        except Exception as exc:
            logger.error(f"Failed to write risk.json: {exc}")
            return False, f"Failed to save risk config: {exc}"

        self._current_preset = preset_name
        self._presets_data["current_preset"] = preset_name
        try:
            _ps = json.dumps(self._presets_data, indent=2)
            _fd2, _tmp2 = tempfile.mkstemp(dir=str(_PRESETS_PATH.parent), suffix=".tmp")
            try:
                with os.fdopen(_fd2, "w", encoding="utf-8") as _tf2:
                    _tf2.write(_ps)
                os.replace(_tmp2, _PRESETS_PATH)
            except Exception:
                try:
                    os.unlink(_tmp2)
                except OSError:
                    pass
                raise
            logger.info(f"Risk preset changed to '{preset_name}'")
            return True, f"Risk preset '{preset_name}' applied successfully"
        except Exception as exc:
            logger.error(f"Failed to save preset selection: {exc}")
            return False, f"Failed to save preset selection: {exc}"
        
    def validate_final_risk(
        self,
        balance: float,
        risk_amount: float,
        max_risk_pct: float,
    ) -> bool:
        actual_pct = (risk_amount / max(balance, 1e-9)) * 100.0

        return actual_pct <= max_risk_pct

# Application-level singleton
risk_manager = RiskManager()