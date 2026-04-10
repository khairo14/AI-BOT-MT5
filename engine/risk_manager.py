"""
Risk Manager — position sizing, SL/TP calculation, drawdown tracking,
circuit breaker, and trade eligibility checks.
Reads defaults from config/risk.json. All values are overridable at runtime.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

CONFIG_PATH = Path(__file__).parent.parent / "config" / "risk.json"
_STATE_PATH = Path(__file__).parent.parent / "data" / "risk_state.json"
_PRESETS_PATH = Path(__file__).parent.parent / "config" / "risk_presets.json"


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

        # Global circuit breaker
        self._daily_halted = False
        self._weekly_halted = False
        self._cb_enabled = True   # can be toggled via API
        # G-3: optional callback — set from api/main.py to broadcast a WS alert
        self._on_circuit_breaker = None   # Callable[[str, str], None] | None

        # RISK-3: timestamp of last mode-switch; used to apply a brief 1-second
        # hold in check_concurrent_limit so in-flight orders from the old mode
        # cannot slip through before the new mode's state is fully settled.
        self._mode_switch_ts: Optional[datetime] = None

        # Lock protecting all mutable state that is accessed from both the
        # async runner loop (update_balance) and _poll_outcome threads.
        self._lock = threading.Lock()

        # GAP-1: restore state from disk so circuit breakers survive restarts.
        # Only auto-load when using the production config (no explicit config dict
        # passed) — tests that supply a config dict get a clean in-memory state.
        if config is None:
            self._load_state()

    # ------------------------------------------------------------------
    # State Persistence (GAP-1)
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
            }
            _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"RiskManager: could not save state: {exc}")

    def _load_state(self) -> None:
        """Restore circuit breaker state from disk if available."""
        if not _STATE_PATH.exists():
            return
        try:
            state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
            self._daily_halted       = bool(state.get("daily_halted", False))
            self._weekly_halted      = bool(state.get("weekly_halted", False))
            self._day_start_balance  = state.get("day_start_balance")
            self._week_start_balance = state.get("week_start_balance")
            self._tracking_date      = date.fromisoformat(state["tracking_date"]) if state.get("tracking_date") else None
            _tw = state.get("tracking_week")
            self._tracking_week = tuple(_tw) if isinstance(_tw, list) and len(_tw) == 2 else _tw
            
            for k, v in state.get("consecutive_losses", {}).items():
                if k in self._consecutive_losses:
                    self._consecutive_losses[k] = int(v)
            for k, v in state.get("paused_modes", {}).items():
                if k in self._paused_modes:
                    self._paused_modes[k] = datetime.fromisoformat(v) if v else None
            logger.info("RiskManager: circuit breaker state restored from disk")
        except Exception as exc:
            logger.warning(f"RiskManager: could not restore state: {exc}")

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
        
        # Apply risk preset multiplier
        preset = self._get_current_preset_config()
        if preset:
            risk_pct *= preset.get("risk_per_trade_multiplier", 1.0)
        
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
            logger.error("calculate_lot_size: SL distance is zero — signal must be rejected upstream")
            return 0.0

        raw_lot = risk_amount / (sl_ticks * _tick_value)

        # Round down to nearest lot_step (never round up — avoids over-risking)
        lot = math.floor(raw_lot / lot_step) * lot_step
        lot = round(lot, 8)

        # Detect when broker minimum lot exceeds intended risk.
        # math.floor can produce 0.0 when raw_lot < lot_step; clamping to min_lot
        # then silently doubles-or-more the intended risk. Log a clear warning so
        # the operator knows actual risk is higher than configured.
        if raw_lot > 0 and lot < min_lot:
            actual_risk_pct = (min_lot / raw_lot) * risk_pct
            logger.warning(
                f"Lot size {raw_lot:.5f} is below broker minimum {min_lot} — "
                f"clamping to {min_lot}. Actual risk will be {actual_risk_pct:.2f}% "
                f"(target {risk_pct:.2f}%). Consider widening SL or reducing position on a larger balance."
            )

        lot = max(min_lot, min(lot, max_lot))

        logger.debug(
            f"Lot size | Balance: {_balance} | Risk: {risk_pct}% "
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

        ATR% = ATR(14) / close * 100 — same metric used by regime classifier.

        Baseline ATR% by type (calm market reference):
          scalping    → 0.15% (M5 candles on major forex)
          day_trading → 0.50% (H1 candles)
          swing       → 1.50% (H4 candles)

        Scaling:
          atr_pct ≤ baseline       → no reduction (factor = 1.0)
          atr_pct = 2× baseline    → factor = 0.75
          atr_pct = 3× baseline    → factor = 0.60
          atr_pct ≥ 4× baseline    → factor = 0.50 (floor)

        This means during a crypto news spike (3× normal vol), position
        size automatically halves — without needing manual intervention.
        """
        _BASELINE_ATR: dict[str, float] = {
            "scalping":    0.15,
            "day_trading": 0.50,
            "swing":       1.50,
        }
        baseline = _BASELINE_ATR.get(trading_type, 0.50)
        if baseline <= 0 or atr_pct <= baseline:
            return lot  # calm market — no reduction

        ratio = atr_pct / baseline
        if ratio <= 1.0:
            factor = 1.0
        elif ratio <= 2.0:
            # Linear scale from 1.0 → 0.75 between 1× and 2× baseline
            factor = 1.0 - (ratio - 1.0) * 0.25
        elif ratio <= 3.0:
            # Linear scale from 0.75 → 0.60 between 2× and 3× baseline
            factor = 0.75 - (ratio - 2.0) * 0.15
        else:
            factor = 0.50  # floor at 50% for extreme volatility

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
        Per-mode minimums are read from risk_reward_min_by_mode (falls back to risk_reward_min).
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
            if rr < min_rr - 1e-9:  # tolerance for floating-point precision
                return False, f"R:R {rr:.2f} is below minimum {min_rr} for {trading_type or 'trade'}"

        return True, ""

    # ------------------------------------------------------------------
    # Drawdown & Circuit Breakers
    # ------------------------------------------------------------------

    def update_balance(self, current_balance: float) -> None:
        """
        Call this after every trade close. Tracks daily/weekly balance
        and triggers circuit breakers if thresholds are exceeded.
        """
        with self._lock:
            # Always use UTC to match forex market day boundaries
            _now_utc  = datetime.now(tz=timezone.utc)
            today     = _now_utc.date()
            _iso      = _now_utc.isocalendar()
            week_key  = (_iso.year, _iso.week)   # tuple prevents year-boundary rollover

            # Reset daily tracking at start of new day
            if self._tracking_date != today:
                self._tracking_date = today
                self._day_start_balance = current_balance
                self._daily_halted = False
                logger.info(f"Daily balance reset: {current_balance}")

            # Reset weekly tracking at start of new week
            if self._tracking_week != week_key:
                self._tracking_week = week_key
                self._week_start_balance = current_balance
                self._weekly_halted = False
                logger.info(f"Weekly balance reset: {current_balance}")

            # Check daily drawdown
            if self._day_start_balance:
                daily_dd = (self._day_start_balance - current_balance) / self._day_start_balance * 100
                daily_limit = self._config["drawdown"]["daily_limit_pct"]
                if self._cb_enabled and daily_dd >= daily_limit and not self._daily_halted:
                    self._daily_halted = True
                    logger.warning(
                        f"CIRCUIT BREAKER: Daily drawdown {daily_dd:.2f}% >= {daily_limit}%. "
                        "All new trades halted for today."
                    )
                    if self._on_circuit_breaker:
                        self._on_circuit_breaker("daily", f"Daily drawdown {daily_dd:.2f}% reached {daily_limit}% limit")

            # Check weekly drawdown
            if self._week_start_balance:
                weekly_dd = (self._week_start_balance - current_balance) / self._week_start_balance * 100
                weekly_limit = self._config["drawdown"]["weekly_limit_pct"]
                if self._cb_enabled and weekly_dd >= weekly_limit and not self._weekly_halted:
                    self._weekly_halted = True
                    logger.warning(
                        f"CIRCUIT BREAKER: Weekly drawdown {weekly_dd:.2f}% >= {weekly_limit}%. "
                        "All new trades halted until next Monday."
                    )
                    if self._on_circuit_breaker:
                        self._on_circuit_breaker("weekly", f"Weekly drawdown {weekly_dd:.2f}% reached {weekly_limit}% limit")
            self._save_state()

    def record_loss(self, trading_mode: str) -> None:
        """Increment consecutive loss counter for a mode. Pauses mode if limit hit."""
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
            self._save_state()

    def record_win(self, trading_mode: str) -> None:
        """Reset consecutive loss counter on a win."""
        with self._lock:
            self._consecutive_losses[trading_mode.lower()] = 0
            self._save_state()

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
            self._save_state()
        logger.info("Circuit breaker: consecutive-loss counters reset.")

    def reset_for_mode_switch(self) -> None:
        """Reset all state when switching between paper and live modes.
        Prevents losses accumulated in one mode from blocking the other."""
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
            # RISK-3: record switch time so check_concurrent_limit enforces a
            # brief settling hold for any in-flight requests targeting old state.
            self._mode_switch_ts = datetime.now(tz=timezone.utc)
            self._save_state()
        logger.info("RiskManager: all state reset for mode switch.")

    def set_circuit_breaker_enabled(self, enabled: bool) -> None:
        """Enable or disable the circuit breaker globally."""
        self._cb_enabled = enabled
        logger.info(f"Circuit breaker {'enabled' if enabled else 'DISABLED'} via API.")

    def is_trading_allowed(self, trading_mode: str) -> tuple[bool, str]:
        """Returns (allowed, reason). Check before opening any new trade."""
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
        """
        Check if a new trade can be opened given current open position counts.
        `open_positions` should be the full list from MT5Client.get_open_positions().
        Pass `symbol` to also enforce the per-symbol concurrent limit.
        """
        with self._lock:
            # RISK-3: if a mode-switch happened in the last 1 second, hold new trades
            # briefly so in-flight orders from the old mode cannot bypass the limit.
            if self._mode_switch_ts is not None:
                elapsed = (datetime.now(tz=timezone.utc) - self._mode_switch_ts).total_seconds()
                if elapsed < 1.0:
                    return False, "Mode switch settling — retry in a moment"
            mode = trading_mode.lower()
            limits = self._config["max_concurrent_trades"]
            mode_limit = limits.get(mode, 999)
            total_limit = limits.get("total", 999)
            per_symbol_limit = limits.get("per_symbol", 999)

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

            if symbol is not None:
                # Scope per-symbol count to the same trading mode so a day-trade
                # position on GBPUSD does NOT block a scalping signal on GBPUSD.
                symbol_count = sum(
                    1 for p in open_positions
                    if p.get("symbol") == symbol and p.get("comment", "").startswith(prefix)
                )
                if symbol_count >= per_symbol_limit:
                    return False, f"Per-symbol limit reached for {symbol} ({symbol_count}/{per_symbol_limit})"

            return True, ""

    # ------------------------------------------------------------------
    # Status
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
    # Risk Presets (Task #12)
    # ------------------------------------------------------------------

    def _get_current_preset_config(self) -> Optional[dict]:
        """Get the configuration for the currently active preset."""
        if not self._presets_data.get("presets"):
            return None
        return self._presets_data["presets"].get(self._current_preset)

    def get_available_presets(self) -> dict:
        """Return all available presets with their configurations."""
        return {
            "current": self._current_preset,
            "presets": self._presets_data.get("presets", {})
        }

    def set_risk_preset(self, preset_name: str) -> tuple[bool, str]:
        """
        Set the active risk preset (conservative/moderate/aggressive).
        Returns (success, message).
        """
        presets = self._presets_data.get("presets", {})
        if preset_name not in presets:
            available = ", ".join(presets.keys())
            return False, f"Unknown preset '{preset_name}'. Available: {available}"

        self._current_preset = preset_name
        self._presets_data["current_preset"] = preset_name

        # Save to disk
        try:
            with open(_PRESETS_PATH, "w") as f:
                json.dump(self._presets_data, f, indent=2)
            logger.info(f"Risk preset changed to '{preset_name}'")
            return True, f"Risk preset set to '{preset_name}'"
        except Exception as exc:
            logger.error(f"Failed to save preset selection: {exc}")
            return False, f"Failed to save: {exc}"

    def get_adjusted_config(self, key: str, default=None):
        """
        Get a config value with preset multipliers applied.
        Useful for drawdown limits, max concurrent trades, etc.
        """
        base_value = self._config.get(key, default)
        preset = self._get_current_preset_config()

        if not preset:
            return base_value

        # Apply preset overrides for specific keys
        if key == "drawdown":
            if isinstance(base_value, dict):
                adjusted = base_value.copy()
                adjusted["daily_limit_pct"] = preset.get("max_daily_loss_pct", base_value.get("daily_limit_pct", 5.0))
                adjusted["weekly_limit_pct"] = preset.get("max_weekly_loss_pct", base_value.get("weekly_limit_pct", 10.0))
                adjusted["max_consecutive_losses"] = preset.get("max_consecutive_losses", base_value.get("max_consecutive_losses", 5))
                return adjusted

        elif key == "max_concurrent_trades":
            return preset.get("max_concurrent_trades", base_value)

        return base_value


# Application-level singleton
risk_manager = RiskManager()