"""
Trade Memory — persistent store for closed trade outcomes.

Each entry records what the RL agent needs to learn from:
  - which strategy + symbol + direction fired
  - signal confidence at entry time
  - actual P&L (pips, money, %)
  - whether SL or TP was hit (outcome type)
  - hold duration in minutes

Stored as a JSONL file: ai/data/trade_memory.jsonl
One JSON object per line for easy appending and streaming reads.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

MEMORY_FILE = DATA_DIR / "trade_memory.jsonl"
VALID_DIRECTIONS = {"BUY", "SELL"}
VALID_TRADING_TYPES = {"scalping", "day_trading", "swing"}

# Broker account execution only.
# paper is allowed only as an alias, then normalized to demo.
VALID_EXECUTION_MODES = {"live", "demo", "paper"}

VALID_OUTCOMES = {
    "tp_hit",
    "sl_hit",
    "manual_close",
    "partial_close",
    "breakeven",
    "unknown",
}

# Only these are safe for RL learning.
LEARNING_OUTCOMES = {"tp_hit", "sl_hit"}

def normalize_execution_mode(value: str) -> str:
    value = str(value or "live").lower().strip()

    if value == "paper":
        return "demo"

    return value
@dataclass
class TradeOutcome:
    ticket:          int
    symbol:          str
    strategy:        str
    trading_type:    str          # scalping | day_trading | swing
    direction:       str          # BUY | SELL
    confidence:      float        # 0–1 score at signal time
    entry_price:     float
    close_price:     float
    sl_price:        float
    tp_price:        float
    volume:          float
    profit:          float        # money P&L (account currency)
    profit_pips:     float        # pips P&L
    profit_pct:      float        # % of account balance at open
    outcome:         str          # "tp_hit" | "sl_hit" | "manual_close" | "unknown"
    open_time:       str          # ISO datetime
    close_time:      str          # ISO datetime
    duration_mins:   float
    mode:            str = ""   # scalping | day_trading | swing
    lstm_predicted_direction: Optional[str] = None  # "BUY" or "SELL" — LSTM prediction at signal time
    regime:          Optional[str] = None   # market regime at signal time (e.g. "trending_bull")
    rl_state:        Optional[str] = None   # RL state bucket at signal time (e.g. "med_high_active_low_tight")
    account_login:   int = 0
    extra:           dict = field(default_factory=dict)
    symbol_raw: str = ""
    symbol_normalized: str = ""
    account_type: str = ""
    user_id: str = "default"
    execution_mode: str = "live"
    learning_valid: bool = False
    validation_errors: list[str] = field(default_factory=list)
    source: str = "live"

class TradeMemory:
    """
    Thread-safe trade outcome store. Appends to JSONL on disk and
    keeps a rolling in-memory buffer for fast RL training reads.
    """

    MAX_BUFFER = 10_000   # keep last N outcomes in memory

    def __init__(self):
        self._lock   = threading.Lock()
        self._buffer: list[dict] = []
        self._load()

    # ── public API ────────────────────────────────────────────────────────────

    def record(self, outcome: TradeOutcome) -> None:
        """Append a closed trade outcome to memory."""
        entry = asdict(outcome)

        try:
            from maintenance_agent import maintenance_agent
            entry = maintenance_agent.validate_trade_outcome(entry)
        except Exception as exc:
            entry["learning_valid"] = False
            entry["validation_errors"] = [f"maintenance_validation_failed:{exc}"]

        entry["recorded_at"] = datetime.now(timezone.utc).isoformat() + "Z"

        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) > self.MAX_BUFFER:
                self._buffer = self._buffer[-self.MAX_BUFFER:]

            with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")

    def _validate_and_normalize(self, entry: dict) -> dict:
        errors = []

        entry["direction"] = str(entry.get("direction", "")).upper().strip()
        entry["outcome"] = str(entry.get("outcome", "unknown")).lower().strip()
        entry["trading_type"] = str(entry.get("trading_type", "")).lower().strip()
        entry["execution_mode"] = normalize_execution_mode(
            entry.get("execution_mode") or entry.get("mode") or "live"
        )

        if not entry.get("user_id"):
            errors.append("missing_user_id")
        if not entry.get("account_login"):
            errors.append("missing_account_login")
        if not entry.get("account_type"):
            errors.append("missing_account_type")
        if not entry.get("symbol_normalized"):
            errors.append("missing_symbol_normalized")
        if not entry.get("strategy"):
            errors.append("missing_strategy")

        if entry["direction"] not in VALID_DIRECTIONS:
            errors.append("invalid_direction")
        if entry["trading_type"] not in VALID_TRADING_TYPES:
            errors.append("invalid_trading_type")
        if entry["execution_mode"] not in VALID_EXECUTION_MODES:
            errors.append("invalid_execution_mode")
        if entry["outcome"] not in VALID_OUTCOMES:
            errors.append("invalid_outcome")

        try:
            confidence = float(entry.get("confidence", 0))
            if not 0 <= confidence <= 1:
                errors.append("confidence_out_of_range")
        except Exception:
            errors.append("invalid_confidence")

        for key in ("entry_price", "close_price", "volume"):
            try:
                if float(entry.get(key, 0)) <= 0:
                    errors.append(f"invalid_{key}")
            except Exception:
                errors.append(f"invalid_{key}")

        try:
            if float(entry.get("duration_mins", 0)) < 0:
                errors.append("invalid_duration")
        except Exception:
            errors.append("invalid_duration")

        entry["validation_errors"] = errors
        entry["learning_valid"] = (
            not errors
            and entry["outcome"] in LEARNING_OUTCOMES
            and entry["execution_mode"] in {"live", "demo", "paper"}
        )

        return entry

    def recent(self,
        n: int = 200,
        trading_type: Optional[str] = None,
        execution_mode: Optional[str] = None,
        account_login: Optional[int] = None,
        account_type: Optional[str] = None,
        user_id: Optional[str] = None,
        strategy: Optional[str] = None,
        symbol_normalized: Optional[str] = None,
        learning_only: bool = False,
        live_only: bool = False,
    ) -> list[dict]:
        """Return the last N outcomes, optionally filtered by trading_type and/or mode.
        live_only=True excludes backtest entries so RL/optimizer aren't skewed by re-runs."""
        with self._lock:
            data = list(self._buffer)

        if trading_type:
            data = [d for d in data if d.get("trading_type") == trading_type]
        if execution_mode:
            data = [d for d in data if d.get("execution_mode", d.get("mode")) == execution_mode]
        if account_login is not None:
            data = [d for d in data if d.get("account_login") == account_login]
        if account_type:
            data = [d for d in data if d.get("account_type") == account_type]
        if user_id:
            data = [d for d in data if d.get("user_id") == user_id]
        if strategy:
            data = [d for d in data if d.get("strategy") == strategy]
        if symbol_normalized:
            data = [d for d in data if d.get("symbol_normalized") == symbol_normalized]
        if learning_only:
            data = [d for d in data if d.get("learning_valid") is True]
        if live_only:
            data = [d for d in data if d.get("execution_mode", d.get("mode")) == "live"]

        return data[-n:]

    def stats(self, trading_type: Optional[str] = None, live_only: bool = False, mode: Optional[str] = None, exclude_manual: bool = False, account_login: Optional[int] = None) -> dict:
        """Aggregate stats used by the RL agent and dashboard."""
        outcomes = self.recent(n=self.MAX_BUFFER, trading_type=trading_type, live_only=live_only, execution_mode=mode, account_login=account_login)
        if exclude_manual:
            outcomes = [
                o for o in outcomes
                if o.get("learning_valid") is True
            ]
        if not outcomes:
            return {"total": 0}
        total   = len(outcomes)
        wins    = sum(1 for o in outcomes if o["profit"] > 0)
        losses  = sum(1 for o in outcomes if o["profit"] < 0)
        avg_pnl = sum(o["profit"] for o in outcomes) / total
        avg_conf= sum(o["confidence"] for o in outcomes) / total
        tp_hits = sum(1 for o in outcomes if o["outcome"] == "tp_hit")
        sl_hits = sum(1 for o in outcomes if o["outcome"] == "sl_hit")
        # Post-slippage EV — subtract avg slippage cost from avg PnL
        # Slippage is stored in extra.slippage_pips; convert to account currency
        # using a conservative pip value estimate (varies by symbol/lot but
        # this gives a directional signal for whether edge survives execution)
        avg_slip_pips = sum(
            o.get("extra", {}).get("slippage_pips", 0.0) for o in outcomes
        ) / total
        # Rough pip value: $1/pip per 0.01 lot on standard account
        # This is an estimate — exact value depends on symbol and lot size
        avg_slip_cost = avg_slip_pips * 1.0  # $1 per pip estimate per trade
        return {
            "total":      total,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   round(wins / total, 4) if total else 0,
            "avg_pnl":    round(avg_pnl, 4),
            "avg_conf":   round(avg_conf, 4),
            "tp_hits":    tp_hits,
            "sl_hits":    sl_hits,
            "avg_slippage_pips":   round(avg_slip_pips, 2),
            "post_slippage_ev":    round(avg_pnl - avg_slip_cost, 4),
            "slippage_tracked":    sum(1 for o in outcomes if o.get("extra", {}).get("slippage_pips", 0) > 0),
        }
    
    def lstm_accuracy(
        self,
        trading_type: Optional[str] = None,
        min_samples: int = 20,
        live_only: bool = True,
        mode: Optional[str] = None,
        account_login: Optional[int] = None,
    ) -> dict:
        """
        Compute live LSTM prediction accuracy by comparing lstm_predicted_direction
        against actual trade outcome direction.

        A prediction is 'correct' when:
          - LSTM predicted BUY and trade was profitable (price went up)
          - LSTM predicted SELL and trade was profitable (price went down)

        Returns dict with accuracy per symbol and overall, or empty if insufficient data.
        Requires lstm_predicted_direction to be populated in TradeOutcome.extra or field.
        mode: if set, restricts to trades from that account mode ("live" or "paper").
        """
        outcomes = self.recent(
            n=self.MAX_BUFFER,
            trading_type=trading_type,
            live_only=live_only,
            execution_mode=mode,
            account_login=account_login,
        )
        # Only consider trades where LSTM prediction was recorded
        tracked = [
            o for o in outcomes
            if o.get("lstm_predicted_direction") or
            o.get("extra", {}).get("lstm_predicted_direction")
        ]
        if len(tracked) < min_samples:
            return {"tracked": len(tracked), "min_samples": min_samples, "sufficient_data": False}

        def _get_pred(o: dict) -> Optional[str]:
            return (
                o.get("lstm_predicted_direction") or
                o.get("extra", {}).get("lstm_predicted_direction")
            )

        correct = 0
        total   = 0
        by_symbol: dict[str, dict] = {}

        for o in tracked:
            pred   = _get_pred(o)
            actual = o.get("direction", "").upper()
            profit = o.get("profit", 0)
            if not pred or not actual:
                continue
            is_correct = (pred == actual and profit > 0) or (pred != actual and profit < 0)
            correct += int(is_correct)
            total   += 1

            sym = o.get("symbol_normalized") or o.get("symbol", "unknown")
            if sym not in by_symbol:
                by_symbol[sym] = {"correct": 0, "total": 0}
            by_symbol[sym]["correct"] += int(is_correct)
            by_symbol[sym]["total"]   += 1

        overall_acc = round(correct / total, 4) if total else 0.0
        sym_accuracy = {
            sym: {
                "accuracy": round(v["correct"] / v["total"], 4),
                "samples":  v["total"],
            }
            for sym, v in by_symbol.items()
            if v["total"] >= 5  # min 5 samples per symbol
        }

        # Flag symbols where live LSTM accuracy is worse than random (< 45%)
        degraded = [
            sym for sym, v in sym_accuracy.items()
            if v["accuracy"] < 0.45
        ]

        return {
            "overall_accuracy": overall_acc,
            "correct":          correct,
            "total":            total,
            "sufficient_data":  True,
            "by_symbol":        sym_accuracy,
            "degraded_symbols": degraded,
        }

    def snapshot_accuracy(
        self,
        trading_type: Optional[str] = None,
        min_samples: int = 10,
        live_only: bool = True,
        mode: Optional[str] = None,
    ) -> None:
        """
        Append a daily accuracy snapshot to history file.
        
        Logs overall LSTM accuracy for time-series tracking and auto-retrain triggers.
        Call this via a scheduled task or after every N closed trades.
        mode: if set, restricts to trades from that account mode ("live" or "paper").
        """
        acc = self.lstm_accuracy(
            trading_type=trading_type,
            min_samples=min_samples,
            live_only=live_only,
            mode=mode,
        )
        
        if not acc.get("sufficient_data"):
            return  # Not enough data yet, skip snapshot
        
        _history_path = DATA_DIR / "lstm_accuracy_history.jsonl"
        
        snapshot = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "trading_type": trading_type or "all",
            "mode": mode or "all",
            "overall_accuracy": acc["overall_accuracy"],
            "correct": acc["correct"],
            "total": acc["total"],
            "by_symbol": acc.get("by_symbol", {}),
            "degraded_symbols": acc.get("degraded_symbols", []),
        }
        
        with open(_history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot) + "\n")

    def stats_by_regime(
        self,
        trading_type: Optional[str] = None,
        min_samples: int = 5,
        live_only: bool = True,
    ) -> dict:
        """
        Compute win rate and avg PnL per regime per strategy.
        Lets you validate whether regime gating is actually improving edge.
        Returns dict keyed by regime → {win_rate, avg_pnl, total, by_strategy}.
        """
        outcomes = self.recent(
            n=self.MAX_BUFFER,
            trading_type=trading_type,
            live_only=live_only,
        )
        # Only trades where regime was recorded
        tracked = [o for o in outcomes if o.get("regime")]
        if not tracked:
            return {"tracked": 0, "sufficient_data": False}

        by_regime: dict[str, dict] = {}
        for o in tracked:
            regime  = o.get("regime", "unknown")
            strat   = o.get("strategy", "unknown")
            profit  = o.get("profit", 0)
            is_win  = profit > 0

            if regime not in by_regime:
                by_regime[regime] = {"wins": 0, "total": 0, "pnl": 0.0, "by_strategy": {}}
            by_regime[regime]["wins"]  += int(is_win)
            by_regime[regime]["total"] += 1
            by_regime[regime]["pnl"]   += profit

            bs = by_regime[regime]["by_strategy"]
            if strat not in bs:
                bs[strat] = {"wins": 0, "total": 0, "pnl": 0.0}
            bs[strat]["wins"]  += int(is_win)
            bs[strat]["total"] += 1
            bs[strat]["pnl"]   += profit

        result = {}
        for regime, data in by_regime.items():
            total = data["total"]
            if total < min_samples:
                continue
            result[regime] = {
                "win_rate":   round(data["wins"] / total, 4),
                "avg_pnl":    round(data["pnl"] / total, 4),
                "total":      total,
                "by_strategy": {
                    s: {
                        "win_rate": round(v["wins"] / v["total"], 4),
                        "avg_pnl":  round(v["pnl"] / v["total"], 4),
                        "total":    v["total"],
                    }
                    for s, v in data["by_strategy"].items()
                    if v["total"] >= min_samples
                },
            }
        return {"tracked": len(tracked), "sufficient_data": True, "by_regime": result}
    
    def detect_drift(
        self,
        trading_type: Optional[str] = None,
        window: int = 30,
        delta: float = 0.005,
        lambda_threshold: float = 10.0,
        live_only: bool = True,
        mode: Optional[str] = None,
    ) -> dict:
        """
        Page-Hinkley drift detection on rolling win rate.
        Detects when the win rate is drifting downward (regime shift / model decay).

        Page-Hinkley test:
          - Tracks cumulative sum of (observed - expected - delta)
          - Raises alarm when cumulative sum exceeds lambda_threshold
          - delta: minimum acceptable mean change (0.005 = 0.5%)
          - lambda_threshold: sensitivity (lower = more sensitive)

        Returns drift detected flag and severity.
        mode: if set, restricts to trades from that account mode (\"live\" or \"paper\").
        """
        outcomes = self.recent(
            n=max(window * 3, 100),
            trading_type=trading_type,
            live_only=live_only,
            execution_mode=mode,
        )
        if len(outcomes) < window:
            return {
                "drift_detected": False,
                "samples": len(outcomes),
                "min_samples": window,
                "sufficient_data": False,
            }

        # Convert outcomes to win (1) / loss (0) series
        results = [1.0 if o.get("profit", 0) > 0 else 0.0 for o in outcomes]

        # Expected win rate from first half of the window (baseline)
        _half = len(results) // 2
        _baseline_wr = sum(results[:_half]) / _half if _half > 0 else 0.55

        # Page-Hinkley cumulative sum on second half
        _ph_sum   = 0.0
        _ph_min   = 0.0
        _ph_max   = 0.0
        _alarm    = False
        _alarm_at = None

        _alarm_direction = None
        for i, r in enumerate(results[_half:]):
            _ph_sum += r - _baseline_wr - delta
            _ph_min  = min(_ph_min, _ph_sum)
            _ph_max  = max(_ph_max, _ph_sum)
            if _ph_max - _ph_sum > lambda_threshold:
                _alarm           = True
                _alarm_direction = "down"
                _alarm_at        = _half + i
                break
            if _ph_sum - _ph_min > lambda_threshold:
                _alarm           = True
                _alarm_direction = "up"
                _alarm_at        = _half + i
                break

        # Rolling win rate over last window bars for severity assessment
        _recent_wr = sum(results[-window:]) / window

        return {
            "drift_detected":    _alarm,
            "drift_direction":   _alarm_direction,
            "drift_at_sample":   _alarm_at,
            "baseline_win_rate": round(_baseline_wr, 4),
            "recent_win_rate":   round(_recent_wr, 4),
            "win_rate_delta":    round(_recent_wr - _baseline_wr, 4),
            "severity":          (
                "high"   if _alarm and _recent_wr < _baseline_wr - 0.15 else
                "medium" if _alarm and _recent_wr < _baseline_wr - 0.08 else
                "low"    if _alarm else
                "none"
            ),
            "samples":           len(results),
            "sufficient_data":   True,
        }

    def rolling_ev_stability(
        self,
        trading_type: Optional[str] = None,
        window: int = 20,
        min_windows: int = 3,
        live_only: bool = True,
        mode: Optional[str] = None,
    ) -> dict:
        """
        Track EV stability across rolling windows.
        Detects whether performance is stable, improving, or degrading
        before it reaches the hard 40% win-rate alert threshold.

        Returns rolling EV per window and a stability assessment.
        Requires at least min_windows × window trades to compute.
        mode: if set, restricts to trades from that account mode ("live" or "paper").
        """
        outcomes = self.recent(
            n=self.MAX_BUFFER,
            trading_type=trading_type,
            live_only=live_only,
            execution_mode=mode,
        )
        needed = window * min_windows
        if len(outcomes) < needed:
            return {
                "sufficient_data": False,
                "samples":         len(outcomes),
                "needed":          needed,
            }

        # Compute rolling window EV and win rate
        windows_data = []
        for i in range(0, len(outcomes) - window + 1, window // 2):  # 50% overlap
            chunk = outcomes[i: i + window]
            if len(chunk) < window:
                break
            chunk_wins = sum(1 for o in chunk if o.get("profit", 0) > 0)
            chunk_ev   = sum(o.get("profit", 0) for o in chunk) / window
            chunk_pips = sum(o.get("profit_pips", 0) for o in chunk) / window
            windows_data.append({
                "window_start": i,
                "win_rate":     round(chunk_wins / window, 4),
                "avg_ev":       round(chunk_ev, 4),
                "avg_pips":     round(chunk_pips, 2),
            })

        if len(windows_data) < min_windows:
            return {"sufficient_data": False, "samples": len(outcomes), "needed": needed}

        recent_windows = windows_data[-min_windows:]
        evs    = [w["avg_ev"]   for w in recent_windows]
        wrs    = [w["win_rate"] for w in recent_windows]

        # Trend: fit linear regression slope over recent windows
        import statistics as _stats
        _n = len(evs)
        _x_mean = (_n - 1) / 2
        _xy = sum((i - _x_mean) * (evs[i] - _stats.mean(evs)) for i in range(_n))
        _xx = sum((i - _x_mean) ** 2 for i in range(_n))
        _ev_slope = _xy / _xx if _xx > 0 else 0.0

        # Classification
        if _ev_slope > 0.01:
            trend = "improving"
        elif _ev_slope < -0.01:
            trend = "degrading"
        else:
            trend = "stable"

        return {
            "sufficient_data":    True,
            "samples":            len(outcomes),
            "windows":            windows_data,
            "recent_avg_wr":      round(_stats.mean(wrs), 4),
            "recent_avg_ev":      round(_stats.mean(evs), 4),
            "ev_slope":           round(_ev_slope, 6),
            "trend":              trend,
            "latest_win_rate":    wrs[-1],
            "earliest_win_rate":  wrs[0],
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ── internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load existing JSONL file into memory buffer at startup."""
        if not MEMORY_FILE.exists():
            return
        parsed = []
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as exc:
            from loguru import logger
            logger.warning(f"TradeMemory: could not open {MEMORY_FILE}: {exc}")
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except Exception as exc:
                from loguru import logger
                logger.warning(f"TradeMemory: skipping corrupt line in {MEMORY_FILE}: {exc}")
        self._buffer = parsed[-self.MAX_BUFFER:]

    def reload(self) -> int:
        """Reload the JSONL file from disk into the in-memory buffer.
        Used after external edits (e.g. backfill scripts) without restarting the API.
        Returns the number of entries loaded."""
        with self._lock:
            self._buffer = []
        self._load()
        with self._lock:
            return len(self._buffer)


# Application-level singleton
memory = TradeMemory()
