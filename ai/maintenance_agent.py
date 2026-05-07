from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path(__file__).parent / "data"

TRADE_MEMORY_FILE = DATA_DIR / "trade_memory.jsonl"
SIGNAL_JOURNAL_FILE = DATA_DIR / "signal_journal.jsonl"
TRADE_STATE_FILE = DATA_DIR / "trade_state.json"

VALID_DIRECTIONS = {"BUY", "SELL"}
VALID_TRADING_TYPES = {"scalping", "day_trading", "swing"}
VALID_EXECUTION_MODES = {"live", "demo", "backtest", "shadow"}
VALID_OUTCOMES = {
    "tp_hit",
    "sl_hit",
    "manual_close",
    "partial_close",
    "breakeven",
    "unknown",
}
VALID_SOURCES = {"live", "demo", "backtest", "shadow"}
LEARNING_OUTCOMES = {"tp_hit", "sl_hit"}

def normalize_execution_mode(value: Any = None) -> str:
    value = str(value or "live").lower().strip()
    return "demo" if value == "paper" else value

@dataclass
class ValidationResult:
    learning_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)


class MaintenanceAgent:
    """
    Safe EVOTRADE maintenance/audit agent.

    This agent does NOT place trades.
    It validates learning records, scans journals, and recommends whether
    RL learning should continue, pause, or require cleanup.
    """

    def __init__(
        self,
        trade_memory_file: Path = TRADE_MEMORY_FILE,
        signal_journal_file: Path = SIGNAL_JOURNAL_FILE,
        trade_state_file: Path = TRADE_STATE_FILE,
    ):
        self.trade_memory_file = trade_memory_file
        self.signal_journal_file = signal_journal_file
        self.trade_state_file = trade_state_file

    # ------------------------------------------------------------------
    # Core validation
    # ------------------------------------------------------------------

    def validate_trade_outcome(self, outcome: dict) -> dict:
        result = self._validate_trade_outcome(outcome)

        return {
            **result.normalized,
            "learning_valid": result.learning_valid,
            "validation_errors": result.errors,
            "validation_warnings": result.warnings,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _validate_trade_outcome(self, outcome: dict) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        o = dict(outcome or {})

        # Normalize aliases
        o["direction"] = str(o.get("direction", "")).upper().strip()
        o["outcome"] = str(o.get("outcome", "unknown")).lower().strip()
        o["trading_type"] = str(
            o.get("trading_type") or o.get("mode") or ""
        ).lower().strip()

        # execution_mode is separate from trading_type.
        # Keep fallback to old "mode" only if it looks like live/paper/demo.
        raw_execution_mode = (
            o.get("execution_mode")
            or o.get("account_mode")
            or o.get("extra", {}).get("execution_mode")
        )
        if not raw_execution_mode and str(o.get("mode", "")).lower() in VALID_EXECUTION_MODES:
            raw_execution_mode = o.get("mode")

        o["execution_mode"] = normalize_execution_mode(raw_execution_mode)
        o["source"] = str(o.get("source") or o["execution_mode"]).lower().strip()

        if o["source"] == "paper":
            o["source"] = "demo"

        if o["source"] not in VALID_SOURCES:
            errors.append("invalid_source")
            
        # Required identity fields
        required_identity = [
            "user_id",
            "account_login",
            "account_type",
            "strategy",
            "symbol_normalized",
        ]
        for key in required_identity:
            if o.get(key) in (None, "", 0):
                errors.append(f"missing_{key}")

        if not o.get("symbol_raw") and o.get("symbol"):
            o["symbol_raw"] = o.get("symbol")

        if not o.get("symbol_normalized") and o.get("symbol"):
            warnings.append("symbol_normalized_missing_fallback_used")
            o["symbol_normalized"] = str(o.get("symbol")).upper().strip()

        # Enum checks
        if o["direction"] not in VALID_DIRECTIONS:
            errors.append("invalid_direction")

        if o["trading_type"] not in VALID_TRADING_TYPES:
            errors.append("invalid_trading_type")

        if o["execution_mode"] not in VALID_EXECUTION_MODES:
            errors.append("invalid_execution_mode")

        if o["outcome"] not in VALID_OUTCOMES:
            errors.append("invalid_outcome")

        # Numeric checks
        self._check_positive_number(o, "ticket", errors)
        self._check_float_range(o, "confidence", 0.0, 1.0, errors)

        for key in ("entry_price", "close_price", "volume"):
            self._check_positive_number(o, key, errors)

        self._check_non_negative_number(o, "duration_mins", errors)

        # Optional but strongly recommended
        for key in ("profit", "profit_pips", "profit_pct"):
            if key not in o:
                warnings.append(f"missing_{key}")

        if o["outcome"] not in LEARNING_OUTCOMES:
            warnings.append("non_learning_outcome")

        if o["execution_mode"] in {"backtest", "shadow"}:
            warnings.append("non_live_learning_source")

        # Learning-valid means safe for RL/optimizer training.
        learning_valid = (
            not errors
            and o["outcome"] in LEARNING_OUTCOMES
            and o["execution_mode"] in {"live", "demo"}
        )

        return ValidationResult(
            learning_valid=learning_valid,
            errors=errors,
            warnings=warnings,
            normalized=o,
        )

    # ------------------------------------------------------------------
    # Journal scanners
    # ------------------------------------------------------------------

    def scan_trade_memory(self) -> dict:
        rows = self._read_jsonl(self.trade_memory_file)

        total = len(rows)
        valid = 0
        invalid = 0
        error_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        by_account: Counter[str] = Counter()
        by_strategy: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()

        poisoned_samples: list[dict] = []

        for idx, row in enumerate(rows):
            if row.get("_corrupt"):
                invalid += 1
                error_counts.update(["corrupt_jsonl_row"])

                if len(poisoned_samples) < 20:
                    poisoned_samples.append({
                        "line": idx + 1,
                        "ticket": None,
                        "symbol": None,
                        "strategy": None,
                        "errors": ["corrupt_jsonl_row"],
                        "warnings": [],
                    })

                continue
            checked = self.validate_trade_outcome(row)

            if checked.get("learning_valid"):
                valid += 1
            else:
                invalid += 1
                if len(poisoned_samples) < 20:
                    poisoned_samples.append({
                        "line": idx + 1,
                        "ticket": row.get("ticket"),
                        "symbol": row.get("symbol") or row.get("symbol_raw"),
                        "strategy": row.get("strategy"),
                        "errors": checked.get("validation_errors", []),
                        "warnings": checked.get("validation_warnings", []),
                    })

            error_counts.update(checked.get("validation_errors", []))
            warning_counts.update(checked.get("validation_warnings", []))

            acct_key = f"{row.get('user_id', 'missing')}:{row.get('account_login', 'missing')}:{row.get('account_type', 'missing')}"
            by_account[acct_key] += 1
            by_strategy[str(row.get("strategy", "missing"))] += 1
            by_symbol[str(row.get("symbol_normalized") or row.get("symbol") or "missing")] += 1

        return {
            "file": str(self.trade_memory_file),
            "total": total,
            "learning_valid": valid,
            "learning_invalid": invalid,
            "learning_valid_rate": round(valid / total, 4) if total else 0.0,
            "error_counts": dict(error_counts),
            "warning_counts": dict(warning_counts),
            "by_account": dict(by_account),
            "by_strategy": dict(by_strategy),
            "by_symbol": dict(by_symbol),
            "sample_invalid_records": poisoned_samples,
        }

    def scan_signal_journal(self) -> dict:
        rows = self._read_jsonl(self.signal_journal_file)

        total = len(rows)
        missing_counts: Counter[str] = Counter()
        by_account: Counter[str] = Counter()
        by_strategy: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()
        by_decision: Counter[str] = Counter()

        required = [
            "user_id",
            "account_login",
            "account_type",
            "execution_mode",
            "trading_type",
            "strategy",
            "symbol_raw",
            "symbol_normalized",
            "direction",
            "confidence",
        ]

        bad_samples: list[dict] = []

        for idx, row in enumerate(rows):
            if row.get("_corrupt"):
                missing_counts.update(["corrupt_jsonl_row"])

                if len(bad_samples) < 20:
                    bad_samples.append({
                        "line": idx + 1,
                        "missing": ["corrupt_jsonl_row"],
                        "symbol": None,
                        "strategy": None,
                    })

                continue

            missing = [key for key in required if row.get(key) in (None, "", 0)]

            if missing:
                missing_counts.update(missing)
                if len(bad_samples) < 20:
                    bad_samples.append({
                        "line": idx + 1,
                        "missing": missing,
                        "symbol": row.get("symbol") or row.get("symbol_raw"),
                        "strategy": row.get("strategy"),
                    })

            acct_key = f"{row.get('user_id', 'missing')}:{row.get('account_login', 'missing')}:{row.get('account_type', 'missing')}"
            by_account[acct_key] += 1
            by_strategy[str(row.get("strategy", "missing"))] += 1
            by_symbol[str(row.get("symbol_normalized") or row.get("symbol") or "missing")] += 1
            by_decision[str(row.get("decision") or row.get("status") or "unknown")] += 1

        return {
            "file": str(self.signal_journal_file),
            "total": total,
            "missing_counts": dict(missing_counts),
            "by_account": dict(by_account),
            "by_strategy": dict(by_strategy),
            "by_symbol": dict(by_symbol),
            "by_decision": dict(by_decision),
            "sample_bad_records": bad_samples,
        }

    def find_learning_poisoning_risks(self) -> dict:
        memory_scan = self.scan_trade_memory()

        high_risk_errors = {
            "missing_user_id",
            "missing_account_login",
            "missing_account_type",
            "missing_strategy",
            "missing_symbol_normalized",
            "invalid_direction",
            "invalid_trading_type",
            "invalid_execution_mode",
            "invalid_outcome",
            "confidence_out_of_range",
            "corrupt_jsonl_row",
        }

        error_counts = memory_scan.get("error_counts", {})
        warning_counts = memory_scan.get("warning_counts", {})

        found_high_risk = {
            key: count
            for key, count in error_counts.items()
            if key in high_risk_errors and count > 0
        }

        non_learning_count = warning_counts.get("non_learning_outcome", 0)
        non_live_count = warning_counts.get("non_live_learning_source", 0)

        should_freeze = bool(found_high_risk)

        return {
            "should_freeze_rl_learning": should_freeze,
            "high_risk_errors": found_high_risk,
            "non_learning_outcome_count": non_learning_count,
            "non_live_learning_source_count": non_live_count,
            "learning_valid_rate": memory_scan.get("learning_valid_rate", 0.0),
            "recommendation": (
                "freeze_rl_learning_until_cleanup"
                if should_freeze
                else "rl_learning_can_continue_with_learning_only_filter"
            ),
        }

    def recommend_rl_freeze(self) -> bool:
        return bool(self.find_learning_poisoning_risks().get("should_freeze_rl_learning"))

    # ------------------------------------------------------------------
    # Optional safe repair helper
    # ------------------------------------------------------------------

    def dry_run_validate_trade_memory(self) -> dict:
        """
        Does not rewrite files.
        Shows what would become learning_valid or invalid.
        """
        rows = self._read_jsonl(self.trade_memory_file)
        checked = [self.validate_trade_outcome(row) for row in rows]

        return {
            "total": len(checked),
            "would_mark_learning_valid": sum(1 for r in checked if r.get("learning_valid")),
            "would_mark_learning_invalid": sum(1 for r in checked if not r.get("learning_valid")),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []

        rows: list[dict] = []

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
                except Exception:
                    rows.append({
                        "_corrupt": True,
                        "_raw": line[:500],
                    })

        return rows

    def _check_positive_number(self, o: dict, key: str, errors: list[str]) -> None:
        try:
            if float(o.get(key, 0)) <= 0:
                errors.append(f"invalid_{key}")
        except Exception:
            errors.append(f"invalid_{key}")

    def _check_non_negative_number(self, o: dict, key: str, errors: list[str]) -> None:
        try:
            if float(o.get(key, 0)) < 0:
                errors.append(f"invalid_{key}")
        except Exception:
            errors.append(f"invalid_{key}")

    def _check_float_range(
        self,
        o: dict,
        key: str,
        min_value: float,
        max_value: float,
        errors: list[str],
    ) -> None:
        try:
            raw = o.get(key)

            if raw is None:
                errors.append(f"invalid_{key}")
                return

            value = float(raw)

            if value < min_value or value > max_value:
                errors.append(f"{key}_out_of_range")

        except (TypeError, ValueError):
            errors.append(f"invalid_{key}")


maintenance_agent = MaintenanceAgent()