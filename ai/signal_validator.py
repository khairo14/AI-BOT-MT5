from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
AI_DATA_DIR = AI_DIR / "data"
ROOT_DATA_DIR = ROOT_DIR / "data"

AI_DATA_DIR.mkdir(parents=True, exist_ok=True)
ROOT_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Active signal journal written by engine.signal_journal / SignalBus / StrategyRunner.
SIGNAL_JOURNAL_FILE = ROOT_DATA_DIR / "signal_journal.jsonl"

# Separate append-only validated shadow/counterfactual result store.
# We do NOT rewrite signal_journal.jsonl in this first safe implementation.
VALIDATED_SIGNAL_FILE = AI_DATA_DIR / "validated_signal_memory.jsonl"

VALIDATION_STATUS_PENDING = "pending"
VALIDATION_STATUS_VALIDATED = "validated"
VALIDATION_STATUS_SKIPPED = "skipped"

NON_EXECUTED_STATUSES = {"shadow", "rejected", "blocked", "missed", "expired"}
VALIDATABLE_DECISIONS = {
    "blocked_by_risk",
    "blocked_by_filter",
    "blocked_by_rl",
    "blocked_by_duplicate",
    "blocked_by_conflict",
    "rejected",
    "expired",
    "shadow",
    "shadow_only",
}


def normalize_execution_mode(value: Any = None) -> str:
    value = str(value or "live").lower().strip()
    return "demo" if value in {"paper", "demo", "test"} else value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


@dataclass
class SignalValidationResult:
    signal_id: str
    validated: bool
    validated_outcome: str = "unknown"  # would_tp | would_sl | expired | no_hit | unknown
    validation_status: str = VALIDATION_STATUS_PENDING
    future_profit_pips: Optional[float] = None
    future_profit_pct: Optional[float] = None
    max_favorable_pips: Optional[float] = None
    max_adverse_pips: Optional[float] = None
    bars_checked: int = 0
    validation_errors: list[str] = field(default_factory=list)
    validated_at: str = field(default_factory=_iso_now)


class SignalValidator:
    """
    Validates non-executed signal_journal entries after enough future candles exist.

    Safety rules:
    - Does not place trades.
    - Does not modify RL qtables.
    - Does not train LSTM.
    - Does not rewrite signal_journal.jsonl.
    - Appends validation results to ai/data/validated_signal_memory.jsonl.

    Terminology:
    - demo/live executed trades remain broker-truth learning candidates after close.
    - signal validation is counterfactual analytics for blocked/rejected/expired/shadow signals.
    """

    def __init__(
        self,
        signal_journal_file: Path = SIGNAL_JOURNAL_FILE,
        validated_signal_file: Path = VALIDATED_SIGNAL_FILE,
    ) -> None:
        self.signal_journal_file = signal_journal_file
        self.validated_signal_file = validated_signal_file
        self.signal_journal_file.parent.mkdir(parents=True, exist_ok=True)
        self.validated_signal_file.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_pending_signals(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return unvalidated, non-executed signals from the active engine journal."""
        signals = self._read_jsonl(self.signal_journal_file)
        already_validated = self._validated_signal_ids()

        pending: list[dict[str, Any]] = []
        for signal in signals:
            if signal.get("_corrupt"):
                continue

            signal_id = self.signal_id(signal)
            if signal_id in already_validated:
                continue

            if bool(signal.get("validated", False)):
                continue

            status = str(signal.get("status") or "").lower().strip()
            decision = str(signal.get("decision") or "").lower().strip()

            # Executed real broker trades are learned from trade_journal/trade_memory,
            # not counterfactual validation.
            if status == "executed" or decision == "executed":
                continue

            if status in NON_EXECUTED_STATUSES or decision in VALIDATABLE_DECISIONS:
                pending.append(signal)

        return pending[-max(1, int(limit)):]

    def load_validated_results(self, limit: int = 10_000) -> list[dict[str, Any]]:
        rows = [r for r in self._read_jsonl(self.validated_signal_file) if not r.get("_corrupt")]
        return rows[-max(1, int(limit)):]

    def validation_index(self) -> dict[str, dict[str, Any]]:
        """Return latest validation result keyed by signal_id."""
        index: dict[str, dict[str, Any]] = {}
        for row in self.load_validated_results(limit=100_000):
            signal_id = str(row.get("signal_id") or "")
            if signal_id:
                index[signal_id] = row
        return index

    def merge_validation_into_signals(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return signal rows with validation result fields merged in."""
        validations = self.validation_index()
        merged: list[dict[str, Any]] = []

        for row in rows:
            payload = dict(row)
            signal_id = self.signal_id(payload)
            payload.setdefault("signal_id", signal_id)

            validation = validations.get(signal_id)
            if validation:
                payload.update({
                    "validated": bool(validation.get("validated", False)),
                    "validated_outcome": validation.get("validated_outcome"),
                    "validation_status": validation.get("validation_status"),
                    "future_profit_pips": validation.get("future_profit_pips"),
                    "future_profit_pct": validation.get("future_profit_pct"),
                    "max_favorable_pips": validation.get("max_favorable_pips"),
                    "max_adverse_pips": validation.get("max_adverse_pips"),
                    "bars_checked": validation.get("bars_checked"),
                    "validation_errors": validation.get("validation_errors", []),
                    "validated_at": validation.get("validated_at"),
                })
            else:
                payload.setdefault("validated", False)
                payload.setdefault("validation_status", VALIDATION_STATUS_PENDING)

            merged.append(payload)

        return merged

    def validate_pending_signals(
        self,
        *,
        max_signals: int = 100,
        client: Any = None,
        min_age_minutes: Optional[int] = None,
    ) -> dict[str, Any]:
        """
        Validate pending signals that are old enough.

        If client is supplied, it must expose get_ohlcv_range().
        If client is omitted, this method creates a temporary MT5Client as fallback.
        """
        pending = self.load_pending_signals(limit=max_signals)
        already_validated = self._validated_signal_ids()

        checked = 0
        validated = 0
        skipped = 0
        not_ready = 0
        errors: list[dict[str, Any]] = []

        for signal in pending:
            signal_id = self.signal_id(signal)

            if signal_id in already_validated:
                skipped += 1
                continue

            if not self.is_ready_for_validation(signal, min_age_minutes=min_age_minutes):
                not_ready += 1
                continue

            checked += 1

            try:
                future_bars = self.fetch_future_bars_for_signal(signal, client=client)
                if not future_bars:
                    skipped += 1
                    errors.append({
                        "signal_id": signal_id,
                        "reason": "missing_future_bars",
                    })
                    continue

                result = self.validate_signal(signal, future_bars)

                if result.get("validated"):
                    self.record_validated(result)
                    validated += 1
                    already_validated.add(signal_id)
                else:
                    skipped += 1
                    errors.append({
                        "signal_id": signal_id,
                        "reason": "validation_failed",
                        "errors": result.get("validation_errors", []),
                    })

            except Exception as exc:
                skipped += 1
                errors.append({
                    "signal_id": signal_id,
                    "reason": f"exception:{exc}",
                })

        return {
            "success": True,
            "checked": checked,
            "validated": validated,
            "skipped": skipped,
            "not_ready": not_ready,
            "pending_seen": len(pending),
            "validated_store": str(self.validated_signal_file),
            "signal_journal": str(self.signal_journal_file),
            "errors": errors[:25],
        }

    def validate_signal(self, signal: dict[str, Any], future_bars: list[dict[str, Any]]) -> dict[str, Any]:
        """
        Validate one signal using future OHLCV bars.

        Conservative rule:
        - If TP and SL are touched inside the same candle, count SL first.
        """
        errors: list[str] = []
        signal_id = self.signal_id(signal)

        signal = dict(signal)
        signal["execution_mode"] = normalize_execution_mode(
            signal.get("execution_mode") or signal.get("account_mode")
        )

        direction = str(signal.get("direction", "")).upper().strip()
        entry = self._safe_float(signal.get("entry") or signal.get("entry_price"))
        sl = self._safe_float(signal.get("sl") or signal.get("sl_price"))
        tp = self._safe_float(signal.get("tp") or signal.get("tp_price"))

        if direction not in {"BUY", "SELL"}:
            errors.append("invalid_direction")
        if entry is None or entry <= 0:
            errors.append("invalid_entry")
        if sl is None or sl <= 0:
            errors.append("invalid_sl")
        if tp is None or tp <= 0:
            errors.append("invalid_tp")
        if not future_bars:
            errors.append("missing_future_bars")

        if errors:
            return SignalValidationResult(
                signal_id=signal_id,
                validated=False,
                validation_status=VALIDATION_STATUS_SKIPPED,
                validation_errors=errors,
            ).__dict__

        assert entry is not None
        assert sl is not None
        assert tp is not None

        outcome = "no_hit"
        max_favorable_price = 0.0
        max_adverse_price = 0.0
        final_close = entry
        bars_checked = 0

        for bar in future_bars:
            high = self._safe_float(bar.get("high"))
            low = self._safe_float(bar.get("low"))
            close = self._safe_float(bar.get("close"))

            if high is None or low is None or close is None:
                continue

            bars_checked += 1
            final_close = close

            if direction == "BUY":
                max_favorable_price = max(max_favorable_price, high - entry)
                max_adverse_price = min(max_adverse_price, low - entry)

                if low <= sl:
                    outcome = "would_sl"
                    final_close = sl
                    break

                if high >= tp:
                    outcome = "would_tp"
                    final_close = tp
                    break

            else:
                max_favorable_price = max(max_favorable_price, entry - low)
                max_adverse_price = min(max_adverse_price, entry - high)

                if high >= sl:
                    outcome = "would_sl"
                    final_close = sl
                    break

                if low <= tp:
                    outcome = "would_tp"
                    final_close = tp
                    break

        future_profit_pips = self._price_to_pips(
            signal,
            final_close - entry,
            direction,
        )

        result = SignalValidationResult(
            signal_id=signal_id,
            validated=True,
            validation_status=VALIDATION_STATUS_VALIDATED,
            validated_outcome=outcome,
            future_profit_pips=future_profit_pips,
            future_profit_pct=None,
            max_favorable_pips=self._price_to_pips(signal, max_favorable_price, direction),
            max_adverse_pips=self._price_to_pips(signal, max_adverse_price, direction),
            bars_checked=bars_checked,
            validation_errors=[],
        )

        return {
            **signal,
            **result.__dict__,
        }

    def record_validated(self, result: dict[str, Any]) -> None:
        """Append a validation result once. Duplicate signal_ids are ignored."""
        signal_id = str(result.get("signal_id") or "")
        if signal_id and signal_id in self._validated_signal_ids():
            return

        payload = {
            **result,
            "recorded_at": _iso_now(),
            "learning_eligible": False,
            "learning_source": "signal_validation_counterfactual",
        }

        with self.validated_signal_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def is_ready_for_validation(
        self,
        signal: dict[str, Any],
        *,
        min_age_minutes: Optional[int] = None,
    ) -> bool:
        """
        Determine if enough wall-clock time has passed before validating.

        Defaults use increased conservative windows:
        - scalping: 6 hours
        - day_trading: 5 days
        - swing: 21 days
        """
        signal_time = self.signal_time(signal)
        if signal_time is None:
            return False

        if min_age_minutes is None:
            min_age_minutes = self.validation_delay_minutes(signal)

        return (_utc_now() - signal_time) >= timedelta(minutes=min_age_minutes)

    def validation_delay_minutes(self, signal: dict[str, Any]) -> int:
        trading_type = str(signal.get("trading_type") or signal.get("mode") or "").lower().strip()
        timeframe = str(signal.get("timeframe") or signal.get("tf") or "").upper().strip()

        # Increased validation windows per user preference.
        if trading_type == "scalping" or timeframe in {"M1", "M5", "M15"}:
            return 6 * 60

        if trading_type == "day_trading" or timeframe in {"M30", "H1"}:
            return 5 * 24 * 60

        if trading_type == "swing" or timeframe in {"H4", "D1", "W1"}:
            return 21 * 24 * 60

        return 24 * 60

    def fetch_future_bars_for_signal(
        self,
        signal: dict[str, Any],
        *,
        client: Any = None,
        lookahead_bars: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        symbol = (
            signal.get("symbol_raw")
            or signal.get("symbol")
            or signal.get("symbol_normalized")
        )

        timeframe = (
            signal.get("timeframe")
            or signal.get("tf")
            or self._timeframe_from_trading_type(signal.get("trading_type"))
        )

        start = self.signal_time(signal)
        if not symbol or start is None:
            return []

        bars = int(lookahead_bars or self.lookahead_bars(signal))
        end = start + self._lookahead_delta(str(timeframe), bars)

        own_client = None
        mt5_client = client

        if mt5_client is None:
            try:
                from engine.mt5_client import MT5Client

                own_client = MT5Client()
                if not own_client.connect():
                    return []
                mt5_client = own_client
            except Exception:
                return []

        try:
            df = mt5_client.get_ohlcv_range(
                symbol=str(symbol),
                timeframe=str(timeframe),
                date_from=start,
                date_to=end,
            )
        finally:
            if own_client is not None:
                try:
                    own_client.disconnect()
                except Exception:
                    pass

        if df is None or getattr(df, "empty", True):
            return []

        rows: list[dict[str, Any]] = [
            {str(key): value for key, value in dict(row).items()}
            for row in df.to_dict("records")
        ]

        return [self._normalize_bar(row) for row in rows]

    def lookahead_bars(self, signal: dict[str, Any]) -> int:
        trading_type = str(signal.get("trading_type") or signal.get("mode") or "").lower().strip()
        timeframe = str(signal.get("timeframe") or signal.get("tf") or "").upper().strip()

        if trading_type == "scalping" or timeframe in {"M1", "M5", "M15"}:
            # 6 hours on M5 = 72 bars. Keep 96 to allow slow movement.
            return 96

        if trading_type == "day_trading" or timeframe in {"M30", "H1"}:
            # 5 trading days on H1-ish data.
            return 120

        if trading_type == "swing" or timeframe in {"H4", "D1", "W1"}:
            # Enough to cover 21 days on H4; D1/W1 naturally produce fewer bars.
            return 126

        return 100

    def signal_id(self, signal: dict[str, Any]) -> str:
        explicit = signal.get("signal_id") or signal.get("id")
        if explicit:
            return str(explicit)

        parts = [
            signal.get("recorded_at") or signal.get("timestamp") or signal.get("signal_time") or "",
            signal.get("account_login") or "",
            signal.get("execution_mode") or signal.get("account_type") or "",
            signal.get("trading_type") or signal.get("mode") or "",
            signal.get("strategy") or "",
            signal.get("symbol_normalized") or signal.get("symbol_raw") or signal.get("symbol") or "",
            signal.get("direction") or "",
            signal.get("entry") or signal.get("entry_price") or "",
            signal.get("sl") or signal.get("sl_price") or "",
            signal.get("tp") or signal.get("tp_price") or "",
            signal.get("decision") or "",
            signal.get("reason") or "",
        ]

        return "|".join(str(p) for p in parts)

    def signal_time(self, signal: dict[str, Any]) -> Optional[datetime]:
        for key in ("recorded_at", "timestamp", "signal_time", "created_at", "time"):
            dt = self._parse_datetime(signal.get(key))
            if dt is not None:
                return dt
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []

        rows: list[dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                        if isinstance(payload, dict):
                            rows.append(payload)
                        else:
                            rows.append({"_corrupt": True, "_line": line_no, "_reason": "row_not_object"})
                    except Exception:
                        rows.append({"_corrupt": True, "_line": line_no, "_reason": "json_decode_error"})
        except Exception as exc:
            rows.append({"_corrupt": True, "_line": 0, "_reason": f"file_read_error:{exc}"})

        return rows

    def _validated_signal_ids(self) -> set[str]:
        rows = self._read_jsonl(self.validated_signal_file)
        return {
            str(r.get("signal_id"))
            for r in rows
            if r.get("signal_id")
        }

    def _normalize_bar(self, row: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in row.items():
            if isinstance(value, datetime):
                normalized[key] = value.isoformat()
            else:
                normalized[key] = value
        return normalized

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

    def _price_to_pips(self, signal: dict[str, Any], price_diff: float, direction: str) -> float:
        """
        Basic pip conversion fallback.

        Later this can be improved by storing pip/tick metadata in signal_journal
        at signal creation time.
        """
        symbol = str(signal.get("symbol_normalized") or signal.get("symbol") or "").upper()
        pip_size = 0.01 if "JPY" in symbol else 0.0001

        if direction == "SELL":
            price_diff = -price_diff

        return round(price_diff / pip_size, 2)

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        if not value:
            return None

        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _timeframe_from_trading_type(self, trading_type: Any) -> str:
        trading_type = str(trading_type or "").lower().strip()

        if trading_type == "scalping":
            return "M5"
        if trading_type == "swing":
            return "H4"

        return "H1"

    def _lookahead_delta(self, timeframe: str, bars: int) -> timedelta:
        tf = str(timeframe or "H1").upper()

        minutes_by_tf = {
            "M1": 1,
            "M2": 2,
            "M5": 5,
            "M15": 15,
            "M30": 30,
            "H1": 60,
            "H4": 240,
            "D1": 1440,
            "W1": 10080,
        }

        minutes = minutes_by_tf.get(tf, 60)
        return timedelta(minutes=minutes * bars)


signal_validator = SignalValidator()
