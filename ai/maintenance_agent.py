from __future__ import annotations

import json
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

AI_DIR = Path(__file__).resolve().parent
ROOT_DIR = AI_DIR.parent
AI_DATA_DIR = AI_DIR / "data"
ENGINE_DATA_DIR = ROOT_DIR / "engine" / "data"
ROOT_DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
LOG_DIR = ROOT_DIR / "logs"
MODELS_DIR = AI_DIR / "models"

TRADE_MEMORY_FILE = AI_DATA_DIR / "trade_memory.jsonl"
SIGNAL_JOURNAL_FILE = ENGINE_DATA_DIR / "signal_journal.jsonl"
TRADE_JOURNAL_FILE = ROOT_DATA_DIR / "trade_journal.jsonl"
TRADE_STATE_FILE = ROOT_DATA_DIR / "trade_state.json"
APP_CONFIG_FILE = CONFIG_DIR / "app.json"
RISK_CONFIG_FILE = CONFIG_DIR / "risk.json"
OPTIMIZED_PARAMS_FILE = CONFIG_DIR / "optimized_params.json"
OPTIMIZER_STATUS_FILE = AI_DATA_DIR / "optimizer_status.json"
LSTM_ACCURACY_HISTORY_FILE = AI_DATA_DIR / "lstm_accuracy_history.jsonl"

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
VALID_SOURCES = {"live", "demo", "backtest", "shadow", "broker"}
LEARNING_OUTCOMES = {"tp_hit", "sl_hit"}
SEVERITY_ORDER = {"ok": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

KNOWN_STRATEGIES = {
    "ema_scalp",
    "bb_squeeze",
    "vwap_reversion",
    "stoch_rsi_pullback",
    "macd_ema_trend",
    "sr_breakout",
    "rsi_divergence",
    "ema_trend_rider",
    "fibonacci_rsi",
    "weekly_breakout",
}


def normalize_execution_mode(value: Any = None) -> str:
    value = str(value or "live").lower().strip()
    if value in {"paper", "test"}:
        return "demo"
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


@dataclass
class ValidationResult:
    learning_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict[str, Any] = field(default_factory=dict)


class MaintenanceAgent:
    """
    Safe EVOTRADE maintenance/audit agent.

    Rules:
    - does not place trades
    - does not mutate RL qtables, LSTM models, or optimizer outputs
    - does not blindly repair learning data
    - repair workflow is dry-run first, backup-before-write, and low-risk only
    """

    def __init__(
        self,
        trade_memory_file: Path = TRADE_MEMORY_FILE,
        signal_journal_file: Path = SIGNAL_JOURNAL_FILE,
        trade_state_file: Path = TRADE_STATE_FILE,
        trade_journal_file: Path = TRADE_JOURNAL_FILE,
    ) -> None:
        self.trade_memory_file = trade_memory_file
        self.signal_journal_file = signal_journal_file
        self.trade_state_file = trade_state_file
        self.trade_journal_file = trade_journal_file

    # ------------------------------------------------------------------
    # Public health/audit entrypoints
    # ------------------------------------------------------------------

    def run_health_audit(
        self,
        client: Any = None,
        *,
        notify: bool = False,
        dry_run: bool = True,
        symbols: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Run a safe read-only maintenance audit.

        `client` should be the shared MT5Client when available. Without it, MT5
        reconciliation/feed checks return `info` instead of failing.
        """
        checks: dict[str, Any] = {
            "learning_poisoning": self.find_learning_poisoning_risks(),
            "trade_memory": self.scan_trade_memory(),
            "signal_journal": self.scan_signal_journal(),
            "strategy_integrity": self.audit_strategy_integrity(),
            "journal_hygiene": self.audit_journal_hygiene(),
            "trade_reconciliation": self.audit_trade_reconciliation(client),
            "trade_state": self.audit_trade_state(client),
            "feed": self.check_stale_ticks(client, symbols=symbols),
            "model_optimizer_freshness": self.audit_model_optimizer_freshness(),
            "runtime_health": self.audit_runtime_health(),
            "config_hygiene": self.audit_config_hygiene(),
            "safe_repairs": self.dry_run_safe_repairs(),
        }

        summary = self._build_health_summary(checks, dry_run=dry_run)
        result = {
            "timestamp": _iso_now(),
            "dry_run": dry_run,
            "severity": summary["severity"],
            "status": summary["status"],
            "counts": summary["counts"],
            "recommendations": summary["recommendations"],
            "dashboard_summary": self._build_dashboard_summary(checks, summary),
            "checks": checks,
        }

        if notify and summary["severity"] in {"error", "critical"}:
            self._notify_health_alert(result)

        return result

    def quick_health(self, client: Any = None) -> dict[str, Any]:
        """Lightweight endpoint-friendly health summary."""
        audit = self.run_health_audit(client=client, notify=False, dry_run=True)
        return {
            "timestamp": audit["timestamp"],
            "severity": audit["severity"],
            "status": audit["status"],
            "counts": audit["counts"],
            "recommendations": audit["recommendations"][:10],
            "dashboard_summary": audit.get("dashboard_summary", {}),
        }

    # ------------------------------------------------------------------
    # Core validation
    # ------------------------------------------------------------------

    def validate_trade_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        result = self._validate_trade_outcome(outcome)
        return {
            **result.normalized,
            "learning_valid": result.learning_valid,
            "validation_errors": result.errors,
            "validation_warnings": result.warnings,
            "validated_at": _iso_now(),
        }

    def _validate_trade_outcome(self, outcome: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        o = dict(outcome or {})

        o["direction"] = str(o.get("direction", "")).upper().strip()
        o["outcome"] = str(o.get("outcome", "unknown")).lower().strip()
        o["trading_type"] = str(o.get("trading_type") or o.get("mode") or "").lower().strip()

        raw_execution_mode = (
            o.get("execution_mode")
            or o.get("account_mode")
            or o.get("account_type")
            or o.get("extra", {}).get("execution_mode")
        )
        if not raw_execution_mode and str(o.get("mode", "")).lower() in VALID_EXECUTION_MODES:
            raw_execution_mode = o.get("mode")

        o["execution_mode"] = normalize_execution_mode(raw_execution_mode)
        o["source"] = normalize_execution_mode(o.get("source") or o["execution_mode"])

        if o["source"] not in VALID_SOURCES:
            errors.append("invalid_source")

        if o["source"] == "broker":
            o["source"] = o["execution_mode"]

        if not o.get("symbol_raw") and o.get("symbol"):
            o["symbol_raw"] = o.get("symbol")

        if not o.get("symbol_normalized") and o.get("symbol"):
            warnings.append("symbol_normalized_missing_fallback_used")
            o["symbol_normalized"] = str(o.get("symbol")).upper().strip()

        for key in ("user_id", "account_login", "account_type", "strategy", "symbol_normalized"):
            if o.get(key) in (None, "", 0):
                errors.append(f"missing_{key}")

        if o["direction"] not in VALID_DIRECTIONS:
            errors.append("invalid_direction")
        if o["trading_type"] not in VALID_TRADING_TYPES:
            errors.append("invalid_trading_type")
        if o["execution_mode"] not in VALID_EXECUTION_MODES:
            errors.append("invalid_execution_mode")
        if o["outcome"] not in VALID_OUTCOMES:
            errors.append("invalid_outcome")

        self._check_positive_number(o, "ticket", errors)
        self._check_float_range(o, "confidence", 0.0, 1.0, errors)
        for key in ("entry_price", "close_price", "volume"):
            self._check_positive_number(o, key, errors)
        self._check_non_negative_number(o, "duration_mins", errors)

        for key in ("profit", "profit_pips", "profit_pct"):
            if key not in o:
                warnings.append(f"missing_{key}")

        if o["outcome"] not in LEARNING_OUTCOMES:
            warnings.append("non_learning_outcome")
        if o["execution_mode"] in {"backtest", "shadow"}:
            warnings.append("non_live_learning_source")

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
    # Existing journal scanners
    # ------------------------------------------------------------------

    def scan_trade_memory(self) -> dict[str, Any]:
        rows = self._read_jsonl(self.trade_memory_file)
        total = len(rows)
        valid = 0
        invalid = 0
        error_counts: Counter[str] = Counter()
        warning_counts: Counter[str] = Counter()
        by_account: Counter[str] = Counter()
        by_strategy: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()
        poisoned_samples: list[dict[str, Any]] = []

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

        severity = self._trade_memory_severity(error_counts, warning_counts)

        return {
            "severity": severity,
            "file": str(self.trade_memory_file),
            "exists": self.trade_memory_file.exists(),
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

    def scan_signal_journal(self) -> dict[str, Any]:
        rows = self._read_jsonl(self.signal_journal_file)
        total = len(rows)
        missing_counts: Counter[str] = Counter()
        by_account: Counter[str] = Counter()
        by_strategy: Counter[str] = Counter()
        by_symbol: Counter[str] = Counter()
        by_decision: Counter[str] = Counter()
        bad_samples: list[dict[str, Any]] = []

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

        for idx, row in enumerate(rows):
            if row.get("_corrupt"):
                missing_counts.update(["corrupt_jsonl_row"])
                if len(bad_samples) < 20:
                    bad_samples.append({"line": idx + 1, "missing": ["corrupt_jsonl_row"], "symbol": None, "strategy": None})
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
            "severity": "warning" if missing_counts else "ok",
            "file": str(self.signal_journal_file),
            "exists": self.signal_journal_file.exists(),
            "total": total,
            "missing_counts": dict(missing_counts),
            "by_account": dict(by_account),
            "by_strategy": dict(by_strategy),
            "by_symbol": dict(by_symbol),
            "by_decision": dict(by_decision),
            "sample_bad_records": bad_samples,
        }

    def find_learning_poisoning_risks(self) -> dict[str, Any]:
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
        found_high_risk = {key: count for key, count in error_counts.items() if key in high_risk_errors and count > 0}
        should_freeze = bool(found_high_risk)

        return {
            "severity": "error" if should_freeze else "ok",
            "should_freeze_rl_learning": should_freeze,
            "high_risk_errors": found_high_risk,
            "non_learning_outcome_count": warning_counts.get("non_learning_outcome", 0),
            "non_live_learning_source_count": warning_counts.get("non_live_learning_source", 0),
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
    # New maintenance checks
    # ------------------------------------------------------------------

    def check_stale_ticks(
        self,
        client: Any = None,
        *,
        symbols: Optional[list[str]] = None,
        stale_seconds: int = 180,
    ) -> dict[str, Any]:
        if client is None:
            return {
                "severity": "info",
                "available": False,
                "reason": "mt5_client_not_provided",
                "stale_count": 0,
                "stale": [],
                "market_context": self._market_context(),
            }

        symbols = symbols or self._default_probe_symbols()
        stale: list[dict[str, Any]] = []
        checked: list[dict[str, Any]] = []
        now = _utc_now()
        market_context = self._market_context(now)

        for symbol in symbols[:25]:
            try:
                tick = client.get_current_price(symbol)
            except Exception as exc:
                stale.append({"symbol": symbol, "reason": f"tick_fetch_failed:{exc}"})
                continue

            if not tick:
                stale.append({"symbol": symbol, "reason": "no_tick"})
                checked.append({"symbol": symbol, "age_seconds": None, "has_bid_ask": False})
                continue

            tick_time = tick.get("time")
            if isinstance(tick_time, str):
                tick_dt = self._parse_dt(tick_time)
            elif isinstance(tick_time, datetime):
                tick_dt = tick_time if tick_time.tzinfo else tick_time.replace(tzinfo=timezone.utc)
            else:
                tick_dt = None

            age_seconds = None
            has_bid_ask = tick.get("bid") is not None and tick.get("ask") is not None
            if tick_dt:
                age_seconds = max(0.0, (now - tick_dt).total_seconds())
                if age_seconds > stale_seconds:
                    stale.append({
                        "symbol": symbol,
                        "age_seconds": round(age_seconds, 1),
                        "reason": "stale_tick",
                    })
            elif not has_bid_ask:
                stale.append({"symbol": symbol, "reason": "missing_tick_time_and_bid_ask"})

            checked.append({
                "symbol": symbol,
                "age_seconds": age_seconds,
                "has_bid_ask": has_bid_ask,
            })

        stale_count = len(stale)
        severity = "ok"
        if stale_count:
            # During weekends/off-session, stale ticks are expected and should not
            # make the whole system unsafe. During market hours, stale feed is an error.
            severity = "info" if market_context.get("market_closed") else "error"

        return {
            "severity": severity,
            "available": True,
            "threshold_seconds": stale_seconds,
            "market_context": market_context,
            "checked_count": len(checked),
            "stale_count": stale_count,
            "checked": checked,
            "stale": stale[:50],
        }

    def audit_trade_reconciliation(self, client: Any = None) -> dict[str, Any]:
        journal_rows = self._read_jsonl(self.trade_journal_file)
        open_rows = [r for r in journal_rows if r.get("event") == "open" and not r.get("_corrupt")]
        close_ticket_keys = {
            self._ticket_key(r)
            for r in journal_rows
            if r.get("event") == "close" and not r.get("_corrupt")
        }
        journal_open_rows = [r for r in open_rows if self._ticket_key(r) not in close_ticket_keys]
        journal_open_tickets = {self._safe_int(r.get("ticket")) for r in journal_open_rows if self._safe_int(r.get("ticket"))}

        if client is None:
            return {
                "severity": "info",
                "available": False,
                "reason": "mt5_client_not_provided",
                "journal_open_count": len(journal_open_rows),
            }

        try:
            live_positions = client.get_open_positions() or []
        except Exception as exc:
            return {"severity": "error", "available": False, "reason": f"open_positions_failed:{exc}"}

        live_tickets = {self._safe_int(p.get("ticket")) for p in live_positions if self._safe_int(p.get("ticket"))}
        journal_open_no_longer_live = [r for r in journal_open_rows if self._safe_int(r.get("ticket")) not in live_tickets]
        live_missing_journal_open = [p for p in live_positions if self._safe_int(p.get("ticket")) not in journal_open_tickets]
        orphan_bot_positions = [p for p in live_missing_journal_open if self._looks_like_bot_position(p)]

        broker_closed_missing_journal_close: list[dict[str, Any]] = []
        for row in journal_open_no_longer_live[:100]:
            ticket = self._safe_int(row.get("ticket"))
            if not ticket:
                continue
            try:
                deals = client.get_deals_by_position(ticket) or []
            except Exception:
                deals = []
            if deals:
                broker_closed_missing_journal_close.append({
                    "ticket": ticket,
                    "symbol": row.get("symbol") or row.get("symbol_raw"),
                    "account_login": row.get("account_login"),
                    "account_mode": normalize_execution_mode(row.get("account_mode") or row.get("account_type")),
                    "reason": "broker_has_close_deal_but_journal_has_no_close",
                })

        severity = "ok"
        if orphan_bot_positions or broker_closed_missing_journal_close:
            severity = "error"
        elif journal_open_no_longer_live or live_missing_journal_open:
            severity = "warning"

        return {
            "severity": severity,
            "available": True,
            "live_position_count": len(live_positions),
            "journal_open_count": len(journal_open_rows),
            "journal_open_tickets_no_longer_live": self._sample_ticket_rows(journal_open_no_longer_live),
            "broker_closed_missing_journal_close": broker_closed_missing_journal_close[:25],
            "live_positions_missing_journal_open": self._sample_positions(live_missing_journal_open),
            "orphan_bot_positions": self._sample_positions(orphan_bot_positions),
            "recommendation": self._reconciliation_recommendation(orphan_bot_positions, broker_closed_missing_journal_close, journal_open_no_longer_live),
        }

    def audit_trade_state(self, client: Any = None) -> dict[str, Any]:
        state = self._read_json_file(self.trade_state_file, default={})
        if not isinstance(state, dict):
            return {"severity": "error", "file": str(self.trade_state_file), "reason": "trade_state_not_object"}

        active_state = []
        closed_left_active = []
        flag_warnings = []
        for key, row in state.items():
            if not isinstance(row, dict):
                flag_warnings.append({"key": key, "reason": "state_row_not_object"})
                continue
            closed = bool(row.get("closed"))
            if not closed:
                active_state.append((key, row))
            if closed and (row.get("trailing_active") or row.get("last_event") in {"opened", "tp1_hit", "trailing_update"}):
                closed_left_active.append({"key": key, "ticket": row.get("ticket"), "last_event": row.get("last_event")})
            if row.get("tp2_hit") and not row.get("tp1_hit"):
                flag_warnings.append({"key": key, "reason": "tp2_hit_without_tp1_hit"})
            if row.get("trailing_active") and not (row.get("break_even_moved") or row.get("tp1_hit")):
                flag_warnings.append({"key": key, "reason": "trailing_active_before_be_or_tp1"})
            if row.get("closed") and not row.get("closed_at") and not row.get("close_time"):
                flag_warnings.append({"key": key, "reason": "closed_without_close_time"})

        live_tickets: set[int] = set()
        if client is not None:
            try:
                live_positions = client.get_open_positions() or []
                live_tickets: set[int] = {int(ticket) for ticket in (p.get("ticket") for p in live_positions) if ticket is not None}
            except Exception:
                live_tickets = set()

        active_not_live = []
        if live_tickets:
            for key, row in active_state:
                ticket = self._ticket_from_state_key(key, row)
                if ticket and ticket not in live_tickets:
                    active_not_live.append({"key": key, "ticket": ticket, "symbol": row.get("symbol_raw") or row.get("symbol")})

        severity = "ok"
        if active_not_live or closed_left_active:
            severity = "error"
        elif flag_warnings:
            severity = "warning"

        return {
            "severity": severity,
            "file": str(self.trade_state_file),
            "exists": self.trade_state_file.exists(),
            "total_state_rows": len(state),
            "active_state_count": len(active_state),
            "active_state_not_live": active_not_live[:25],
            "closed_tickets_left_active": closed_left_active[:25],
            "flag_warnings": flag_warnings[:50],
        }

    def audit_strategy_integrity(self) -> dict[str, Any]:
        """Report unknown/truncated strategy names without mutating data."""
        sources = [
            ("trade_memory", self.trade_memory_file),
            ("trade_journal", self.trade_journal_file),
        ]
        counts: Counter[str] = Counter()
        samples: list[dict[str, Any]] = []

        for source_name, path in sources:
            for idx, row in enumerate(self._read_jsonl(path)):
                if row.get("_corrupt"):
                    continue
                strategy = str(row.get("strategy") or row.get("comment") or "").strip()
                if not strategy:
                    continue
                if strategy in KNOWN_STRATEGIES:
                    continue
                # Comments like "day:sr_breakout" or old prefixed comments should still be inspected.
                normalized_guess = self._suggest_strategy_name(strategy)
                counts[strategy] += 1
                if len(samples) < 25:
                    samples.append({
                        "source": source_name,
                        "line": idx + 1,
                        "ticket": row.get("ticket"),
                        "symbol": row.get("symbol_normalized") or row.get("symbol") or row.get("symbol_raw"),
                        "strategy": strategy,
                        "suggested_strategy": normalized_guess,
                        "reason": "unknown_or_truncated_strategy",
                    })

        severity = "warning" if counts else "ok"
        return {
            "severity": severity,
            "known_strategy_count": len(KNOWN_STRATEGIES),
            "unknown_strategy_count": sum(counts.values()),
            "unknown_strategy_names": dict(counts),
            "samples": samples,
            "safe_action": "report_only_no_mutation",
        }

    def audit_journal_hygiene(self) -> dict[str, Any]:
        files = [
            self.trade_memory_file,
            self.signal_journal_file,
            self.trade_journal_file,
            self.trade_state_file,
            LSTM_ACCURACY_HISTORY_FILE,
        ]
        file_reports = []
        worst = "ok"
        for path in files:
            report = self._audit_file_hygiene(path)
            file_reports.append(report)
            worst = self._max_severity(worst, report.get("severity", "ok"))

        duplicate_report = self._audit_duplicate_trade_events()
        worst = self._max_severity(worst, duplicate_report.get("severity", "ok"))

        return {
            "severity": worst,
            "files": file_reports,
            "duplicate_trade_events": duplicate_report,
        }

    def audit_model_optimizer_freshness(self) -> dict[str, Any]:
        now = _utc_now()
        stale_models = []
        failed_models = []
        model_meta_files = sorted(MODELS_DIR.glob("*_meta.json")) if MODELS_DIR.exists() else []

        for meta_path in model_meta_files:
            if meta_path.name.endswith("_anchor_meta.json"):
                continue
            meta = self._read_json_file(meta_path, default={})
            trained_at = self._parse_dt(str(meta.get("trained_at") or meta.get("created_at") or "")) if isinstance(meta, dict) else None
            age_days = (now - trained_at).days if trained_at else None
            if age_days is None:
                failed_models.append({"file": str(meta_path), "reason": "missing_or_invalid_trained_at"})
            elif age_days > 30:
                stale_models.append({"file": str(meta_path), "age_days": age_days, "accuracy": meta.get("accuracy")})
            if isinstance(meta, dict) and meta.get("last_rejection"):
                failed_models.append({"file": str(meta_path), "reason": "last_training_rejected", "last_rejection": meta.get("last_rejection")})

        accuracy_report = self._latest_jsonl_age(LSTM_ACCURACY_HISTORY_FILE, "timestamp", stale_days=7)
        optimizer_report = self._optimizer_freshness_report()

        severity = "ok"
        if failed_models or optimizer_report.get("failed_jobs"):
            severity = "warning"
        if stale_models or accuracy_report.get("stale"):
            severity = self._max_severity(severity, "warning")
        if not MODELS_DIR.exists() or not model_meta_files:
            severity = self._max_severity(severity, "info")

        return {
            "severity": severity,
            "models_dir": str(MODELS_DIR),
            "model_meta_count": len(model_meta_files),
            "stale_model_count": len(stale_models),
            "failed_or_rejected_model_count": len(failed_models),
            "stale_models": stale_models[:20],
            "failed_or_rejected_models": failed_models[:20],
            "accuracy_history": accuracy_report,
            "optimizer": optimizer_report,
        }

    def audit_runtime_health(self) -> dict[str, Any]:
        signal_scan = self.scan_signal_journal()
        decision_counts = signal_scan.get("by_decision", {})
        total = int(signal_scan.get("total") or 0)
        rejected = sum(int(decision_counts.get(k, 0) or 0) for k in ("blocked_by_rl", "blocked_by_risk", "blocked_by_filter", "skipped"))
        rejection_rate = round(rejected / total, 4) if total else 0.0

        log_report = self._audit_logs_for_runtime_errors()
        pending_report = self._pending_signal_file_report()
        severity = "ok"
        if rejection_rate >= 0.75 and total >= 50:
            severity = "warning"
        severity = self._max_severity(severity, log_report.get("severity", "ok"))
        severity = self._max_severity(severity, pending_report.get("severity", "ok"))

        return {
            "severity": severity,
            "signal_total": total,
            "rejected_or_blocked": rejected,
            "rejection_rate": rejection_rate,
            "decision_counts": decision_counts,
            "logs": log_report,
            "pending_signals": pending_report,
        }

    def audit_config_hygiene(self) -> dict[str, Any]:
        app_cfg = self._read_json_file(APP_CONFIG_FILE, default={})
        risk_cfg = self._read_json_file(RISK_CONFIG_FILE, default={})
        issues = []

        if isinstance(app_cfg, dict):
            accounts = app_cfg.get("accounts")
            if isinstance(accounts, dict) and "paper" in accounts:
                issues.append({"severity": "warning", "code": "paper_account_key_present", "message": "app.json still has accounts.paper; paper is retired, use demo/live."})
            ntfy_cfg = ((app_cfg.get("notifications") or {}).get("ntfy") if isinstance(app_cfg.get("notifications"), dict) else None)
            if not isinstance(ntfy_cfg, dict) or not ntfy_cfg.get("enabled"):
                issues.append({"severity": "info", "code": "ntfy_disabled_or_missing"})
            for mode, exec_mode in (app_cfg.get("execution_mode") or {}).items() if isinstance(app_cfg.get("execution_mode"), dict) else []:
                if mode not in VALID_TRADING_TYPES:
                    issues.append({"severity": "warning", "code": "unknown_execution_mode_key", "mode": mode})
                if exec_mode not in {"auto", "manual", "disabled"}:
                    issues.append({"severity": "warning", "code": "unexpected_execution_mode_value", "mode": mode, "value": exec_mode})
        else:
            issues.append({"severity": "error", "code": "app_json_invalid"})

        if isinstance(risk_cfg, dict):
            risk_pct = self._safe_float(risk_cfg.get("risk_per_trade_pct"))
            max_risk = self._safe_float(risk_cfg.get("max_risk_per_trade_pct"))
            if risk_pct is not None and max_risk is not None and risk_pct > max_risk:
                issues.append({"severity": "error", "code": "risk_per_trade_exceeds_max"})
            if not risk_cfg.get("sl_required", True):
                issues.append({"severity": "error", "code": "sl_not_required"})
        else:
            issues.append({"severity": "error", "code": "risk_json_invalid"})

        severity = "ok"
        for issue in issues:
            severity = self._max_severity(severity, issue.get("severity", "ok"))

        return {
            "severity": severity,
            "app_config": str(APP_CONFIG_FILE),
            "risk_config": str(RISK_CONFIG_FILE),
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # Safe repair workflow: dry-run only for now
    # ------------------------------------------------------------------

    def dry_run_validate_trade_memory(self) -> dict[str, Any]:
        rows = self._read_jsonl(self.trade_memory_file)
        checked = [self.validate_trade_outcome(row) for row in rows if not row.get("_corrupt")]
        corrupt = sum(1 for row in rows if row.get("_corrupt"))
        return {
            "total": len(rows),
            "corrupt_rows": corrupt,
            "would_mark_learning_valid": sum(1 for r in checked if r.get("learning_valid")),
            "would_mark_learning_invalid": sum(1 for r in checked if not r.get("learning_valid")),
            "writes_performed": False,
        }

    def dry_run_safe_repairs(self) -> dict[str, Any]:
        """List low-risk repair actions. Does not write anything."""
        actions = []
        for path in (self.trade_memory_file, self.signal_journal_file, self.trade_journal_file, LSTM_ACCURACY_HISTORY_FILE):
            hygiene = self._audit_file_hygiene(path)
            if hygiene.get("corrupt_rows", 0) > 0:
                actions.append({
                    "action": "quarantine_corrupt_jsonl_rows",
                    "file": str(path),
                    "corrupt_rows": hygiene.get("corrupt_rows"),
                    "requires_backup": True,
                    "risk": "low",
                    "writes_performed": False,
                })
            if hygiene.get("oversized"):
                actions.append({
                    "action": "archive_old_jsonl_rows",
                    "file": str(path),
                    "size_mb": hygiene.get("size_mb"),
                    "requires_backup": True,
                    "risk": "low",
                    "writes_performed": False,
                })

        config_hygiene = self.audit_config_hygiene()
        for issue in config_hygiene.get("issues", []):
            if issue.get("code") == "paper_account_key_present":
                actions.append({
                    "action": "manual_config_cleanup_recommended",
                    "file": str(APP_CONFIG_FILE),
                    "issue": issue,
                    "risk": "manual_review",
                    "writes_performed": False,
                })

        return {
            "severity": "info" if actions else "ok",
            "dry_run": True,
            "writes_performed": False,
            "safe_actions_available": len(actions),
            "actions": actions,
            "blocked_actions": [
                "modify_rl_qtables",
                "modify_lstm_model_files",
                "modify_optimizer_outputs",
                "mark_recovered_old_account_trades_learning_valid",
                "place_or_close_trades",
            ],
        }

    def backup_file(self, path: Path) -> Optional[Path]:
        """Backup helper for future explicit repair workflow. Not called automatically."""
        if not path.exists():
            return None
        backup_dir = ROOT_DATA_DIR / "maintenance_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, target)
        return target

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            rows.append(payload)
                        else:
                            rows.append({"_corrupt": True, "_line": line_no, "_raw": raw[:500], "_reason": "jsonl_row_not_object"})
                    except Exception:
                        rows.append({"_corrupt": True, "_line": line_no, "_raw": raw[:500], "_reason": "json_decode_error"})
        except Exception as exc:
            return [{"_corrupt": True, "_line": 0, "_raw": "", "_reason": f"file_read_error:{exc}"}]
        return rows

    def _read_json_file(self, path: Path, *, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return default

    def _audit_file_hygiene(self, path: Path) -> dict[str, Any]:
        exists = path.exists()
        size_bytes = path.stat().st_size if exists else 0
        size_mb = round(size_bytes / (1024 * 1024), 2)
        oversized = size_mb > 100
        report: dict[str, Any] = {
            "file": str(path),
            "exists": exists,
            "size_mb": size_mb,
            "oversized": oversized,
            "severity": "warning" if oversized else "ok",
        }

        if path.suffix == ".jsonl":
            rows = self._read_jsonl(path)
            corrupt = sum(1 for row in rows if row.get("_corrupt"))
            report.update({"rows": len(rows), "corrupt_rows": corrupt})
            if corrupt:
                report["severity"] = "error"
                report["sample_corrupt"] = [r for r in rows if r.get("_corrupt")][:5]
        elif path.suffix == ".json" and exists:
            parsed = self._read_json_file(path, default=None)
            if parsed is None:
                report["severity"] = "error"
                report["corrupt"] = True
            else:
                report["corrupt"] = False
        return report

    def _audit_duplicate_trade_events(self) -> dict[str, Any]:
        rows = self._read_jsonl(self.trade_journal_file)
        counter: Counter[tuple[Any, ...]] = Counter()
        for row in rows:
            if row.get("_corrupt"):
                continue
            key = (
                row.get("account_mode") or row.get("account_type"),
                row.get("account_login"),
                row.get("ticket"),
                row.get("event"),
                row.get("close_time") if row.get("event") != "open" else row.get("open_time"),
                round(self._safe_float(row.get("profit")) or 0.0, 2) if row.get("profit") is not None else None,
            )
            counter[key] += 1
        dupes = [{"key": list(k), "count": v} for k, v in counter.items() if v > 1]
        return {
            "severity": "warning" if dupes else "ok",
            "duplicate_count": len(dupes),
            "samples": dupes[:25],
        }

    def _latest_jsonl_age(self, path: Path, timestamp_key: str, *, stale_days: int) -> dict[str, Any]:
        rows = [r for r in self._read_jsonl(path) if not r.get("_corrupt")]
        latest = None
        for row in rows:
            dt = self._parse_dt(str(row.get(timestamp_key) or ""))
            if dt and (latest is None or dt > latest):
                latest = dt
        age_days = (_utc_now() - latest).days if latest else None
        return {
            "file": str(path),
            "exists": path.exists(),
            "rows": len(rows),
            "latest": latest.isoformat() if latest else None,
            "age_days": age_days,
            "stale": age_days is not None and age_days > stale_days,
            "stale_days_threshold": stale_days,
        }

    def _optimizer_freshness_report(self) -> dict[str, Any]:
        status = self._read_json_file(OPTIMIZER_STATUS_FILE, default={})
        optimized = self._read_json_file(OPTIMIZED_PARAMS_FILE, default={})
        failed_jobs = []
        stale_jobs = []
        now = _utc_now()

        if isinstance(status, dict):
            for key, row in status.items():
                if not isinstance(row, dict):
                    continue
                if row.get("status") in {"failed", "error"} or row.get("error"):
                    failed_jobs.append({"key": key, "error": row.get("error"), "status": row.get("status")})
                dt = self._parse_dt(str(row.get("completed_at") or row.get("updated_at") or row.get("timestamp") or ""))
                if dt and (now - dt).days > 14:
                    stale_jobs.append({"key": key, "age_days": (now - dt).days})

        return {
            "status_file": str(OPTIMIZER_STATUS_FILE),
            "optimized_params_file": str(OPTIMIZED_PARAMS_FILE),
            "status_exists": OPTIMIZER_STATUS_FILE.exists(),
            "optimized_params_exists": OPTIMIZED_PARAMS_FILE.exists(),
            "status_jobs": len(status) if isinstance(status, dict) else 0,
            "optimized_strategy_count": len(optimized) if isinstance(optimized, dict) else 0,
            "failed_jobs": failed_jobs[:50],
            "stale_jobs": stale_jobs[:50],
        }

    def _audit_logs_for_runtime_errors(self) -> dict[str, Any]:
        if not LOG_DIR.exists():
            return {"severity": "info", "available": False, "reason": "logs_dir_missing"}
        log_files = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        patterns = ("order_send failed", "risk", "rejected", "exception", "traceback", "mt5 reconnect failed")
        counts: Counter[str] = Counter()
        samples: list[str] = []
        for path in log_files:
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()[-1000:]
            except Exception:
                continue
            for line in lines:
                lower = line.lower()
                for pattern in patterns:
                    if pattern in lower:
                        counts[pattern] += 1
                        if len(samples) < 10:
                            samples.append(line[-300:])
        severity = "warning" if counts.get("order_send failed", 0) or counts.get("traceback", 0) else "ok"
        return {"severity": severity, "available": True, "files_scanned": [str(p) for p in log_files], "counts": dict(counts), "samples": samples}

    def _pending_signal_file_report(self) -> dict[str, Any]:
        path = ROOT_DATA_DIR / "pending_signals.json"
        data = self._read_json_file(path, default=[])
        count = len(data) if isinstance(data, list) else (len(data) if isinstance(data, dict) else 0)
        severity = "warning" if count > 100 else "ok"
        return {"severity": severity, "file": str(path), "exists": path.exists(), "pending_count": count}

    def _default_probe_symbols(self) -> list[str]:
        symbols: list[str] = []
        for cfg_name in ("scanner.json", "symbols.json"):
            cfg = self._read_json_file(CONFIG_DIR / cfg_name, default={})
            if not isinstance(cfg, dict):
                continue
            for mode in VALID_TRADING_TYPES:
                entry = cfg.get(mode)
                if isinstance(entry, dict):
                    for sym in entry.get("symbols", []):
                        if sym and sym not in symbols:
                            symbols.append(str(sym))
                elif isinstance(entry, list):
                    for item in entry:
                        sym = item.get("symbol") if isinstance(item, dict) else item
                        if sym and sym not in symbols:
                            symbols.append(str(sym))
        return symbols[:10] or ["EURUSD", "GBPUSD", "USDJPY"]

    def _trade_memory_severity(self, error_counts: Counter[str], warning_counts: Counter[str]) -> str:
        if not error_counts and not warning_counts:
            return "ok"

        # invalid_source usually means imported/recovered/old-account rows are blocked
        # from learning. That is important, but not immediately unsafe as long as
        # learning_valid filtering is respected.
        controlled_errors = {"invalid_source"}
        if set(error_counts).issubset(controlled_errors):
            return "warning"

        return "error" if error_counts else "warning"

    def _market_context(self, now: Optional[datetime] = None) -> dict[str, Any]:
        now = now or _utc_now()
        weekday = now.weekday()  # Monday=0, Sunday=6
        hour = now.hour

        # Conservative broker/feed context. Forex and many CFDs are closed from
        # late Friday through Sunday. Crypto may still move, but the probe list is
        # mostly scanner symbols, so we avoid raising hard feed errors on weekends.
        weekend_closed = weekday in {5, 6} or (weekday == 4 and hour >= 22) or (weekday == 0 and hour < 1)
        return {
            "utc_now": now.isoformat(),
            "weekday": weekday,
            "market_closed": weekend_closed,
            "reason": "weekend_or_broker_rollover" if weekend_closed else "regular_market_hours_assumed",
        }

    def _suggest_strategy_name(self, value: str) -> Optional[str]:
        raw = str(value or "").strip().lower()

        for known in KNOWN_STRATEGIES:
            if raw == known.lower():
                return known

        return None

    def _build_dashboard_summary(self, checks: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
        trade_memory = checks.get("trade_memory", {}) if isinstance(checks.get("trade_memory"), dict) else {}
        strategy_integrity = checks.get("strategy_integrity", {}) if isinstance(checks.get("strategy_integrity"), dict) else {}
        feed = checks.get("feed", {}) if isinstance(checks.get("feed"), dict) else {}
        recon = checks.get("trade_reconciliation", {}) if isinstance(checks.get("trade_reconciliation"), dict) else {}
        state = checks.get("trade_state", {}) if isinstance(checks.get("trade_state"), dict) else {}
        model = checks.get("model_optimizer_freshness", {}) if isinstance(checks.get("model_optimizer_freshness"), dict) else {}
        runtime = checks.get("runtime_health", {}) if isinstance(checks.get("runtime_health"), dict) else {}
        poison = checks.get("learning_poisoning", {}) if isinstance(checks.get("learning_poisoning"), dict) else {}

        error_counts = trade_memory.get("error_counts", {}) if isinstance(trade_memory.get("error_counts"), dict) else {}
        invalid_learning_rows = sum(int(v or 0) for v in error_counts.values())

        return {
            "system_status": summary.get("status"),
            "severity": summary.get("severity"),
            "feed_ok": feed.get("severity") in {"ok", "info"},
            "feed_stale_count": int(feed.get("stale_count") or 0),
            "market_closed": bool((feed.get("market_context") or {}).get("market_closed")),
            "trade_reconciliation_ok": recon.get("severity") == "ok",
            "trade_state_ok": state.get("severity") == "ok",
            "journal_hygiene_ok": (checks.get("journal_hygiene") or {}).get("severity") == "ok" if isinstance(checks.get("journal_hygiene"), dict) else False,
            "learning_valid_rate": trade_memory.get("learning_valid_rate", 0.0),
            "invalid_learning_rows": invalid_learning_rows,
            "unknown_strategy_count": int(strategy_integrity.get("unknown_strategy_count") or 0),
            "should_freeze_rl_learning": bool(poison.get("should_freeze_rl_learning")),
            "stale_model_count": int(model.get("stale_model_count") or len(model.get("stale_models", []) or [])),
            "failed_model_count": int(model.get("failed_or_rejected_model_count") or len(model.get("failed_or_rejected_models", []) or [])),
            "runtime_rejection_rate": runtime.get("rejection_rate", 0.0),
            "pending_signal_count": ((runtime.get("pending_signals") or {}).get("pending_count") if isinstance(runtime.get("pending_signals"), dict) else 0),
        }

    def _build_health_summary(self, checks: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
        counts = {"ok": 0, "info": 0, "warning": 0, "error": 0, "critical": 0}
        recommendations: list[str] = []
        severity = "ok"

        for name, check in checks.items():
            check_sev = check.get("severity", "ok") if isinstance(check, dict) else "ok"
            if check_sev not in counts:
                check_sev = "info"
            counts[check_sev] += 1
            severity = self._max_severity(severity, check_sev)

        poison = checks.get("learning_poisoning", {})
        if poison.get("should_freeze_rl_learning"):
            recommendations.append("Freeze RL learning reads to learning_valid=True only until trade_memory cleanup is reviewed.")

        recon = checks.get("trade_reconciliation", {})
        if recon.get("broker_closed_missing_journal_close"):
            recommendations.append("Run existing trade recovery/reconciliation flow; broker-closed trades are missing journal close rows.")
        if recon.get("orphan_bot_positions"):
            recommendations.append("Review orphan bot positions manually before any action; maintenance agent must not close or place trades.")

        state = checks.get("trade_state", {})
        if state.get("active_state_not_live"):
            recommendations.append("Review trade_state active rows not found in MT5 live positions; do not delete until broker history is confirmed.")

        feed = checks.get("feed", {})
        if feed.get("stale_count") and (feed.get("market_context") or {}).get("market_closed"):
            recommendations.append("Feed ticks are stale because market appears closed/weekend; treat as informational unless it persists after market open.")
        elif feed.get("stale_count"):
            recommendations.append("MT5 feed appears stale during assumed market hours; verify Market Watch, broker connection, and runner health.")

        strategy_integrity = checks.get("strategy_integrity", {})
        if strategy_integrity.get("unknown_strategy_count"):
            recommendations.append("Review unknown/truncated strategy names in journals; report-only for now, do not rewrite learning rows blindly.")

        config = checks.get("config_hygiene", {})
        for issue in config.get("issues", []):
            if issue.get("code") == "paper_account_key_present":
                recommendations.append("Clean app.json accounts.paper later; paper is retired and should be demo/live only.")

        if dry_run:
            recommendations.append("All maintenance repairs remain dry-run. Backup before any future write.")

        return {
            "severity": severity,
            "status": "healthy" if severity in {"ok", "info"} else ("needs_attention" if severity == "warning" else "unsafe"),
            "counts": counts,
            "recommendations": recommendations,
        }

    def _notify_health_alert(self, audit: dict[str, Any]) -> None:
        try:
            from engine.notification_manager import notification_manager

            notification_manager.add(
                type="risk_alert",
                title="Maintenance Audit Alert",
                message=(
                    f"EVOTRADE maintenance audit severity: {audit.get('severity')}. "
                    f"Warnings/errors found. Open System Monitoring for details."
                ),
                severity="error" if audit.get("severity") in {"error", "critical"} else "warning",
                metadata={
                    "source": "maintenance_agent",
                    "severity": audit.get("severity"),
                    "counts": audit.get("counts", {}),
                    "timestamp": audit.get("timestamp"),
                },
            )
        except Exception:
            pass

    def _reconciliation_recommendation(self, orphan_bot_positions: list[dict[str, Any]], broker_closed_missing: list[dict[str, Any]], journal_not_live: list[dict[str, Any]]) -> str:
        if orphan_bot_positions:
            return "manual_review_orphan_bot_positions"
        if broker_closed_missing:
            return "run_existing_recovery_to_backfill_close_rows"
        if journal_not_live:
            return "verify_broker_history_for_journal_open_tickets"
        return "journal_and_live_positions_look_consistent"

    def _looks_like_bot_position(self, pos: dict[str, Any]) -> bool:
        comment = str(pos.get("comment") or "").lower()
        if comment.startswith(("scalp", "day", "swing")):
            return True
        try:
            app_cfg = self._read_json_file(APP_CONFIG_FILE, default={})
            magic = int(app_cfg.get("bot_magic", 0)) if isinstance(app_cfg, dict) else 0
        except Exception:
            magic = 0
        return bool(magic and self._safe_int(pos.get("magic")) == magic)

    def _sample_ticket_rows(self, rows: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
        return [
            {
                "ticket": r.get("ticket"),
                "symbol": r.get("symbol") or r.get("symbol_raw"),
                "direction": r.get("direction"),
                "account_login": r.get("account_login"),
                "account_mode": r.get("account_mode") or r.get("account_type"),
                "trading_type": r.get("trading_type"),
            }
            for r in rows[:limit]
        ]

    def _sample_positions(self, positions: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
        return [
            {
                "ticket": p.get("ticket"),
                "symbol": p.get("symbol"),
                "type": p.get("type"),
                "volume": p.get("volume"),
                "profit": p.get("profit"),
                "comment": p.get("comment"),
                "magic": p.get("magic"),
            }
            for p in positions[:limit]
        ]

    def _ticket_key(self, row: dict[str, Any]) -> tuple[str, int, int]:
        return (
            normalize_execution_mode(row.get("account_mode") or row.get("account_type")),
            self._safe_int(row.get("account_login")) or 0,
            self._safe_int(row.get("ticket")) or 0,
        )

    def _ticket_from_state_key(self, key: str, row: dict[str, Any]) -> Optional[int]:
        ticket = self._safe_int(row.get("ticket"))
        if ticket:
            return ticket
        try:
            return int(str(key).split(":")[-1])
        except Exception:
            return None

    def _parse_dt(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _safe_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _max_severity(self, a: str, b: str) -> str:
        return a if SEVERITY_ORDER.get(a, 0) >= SEVERITY_ORDER.get(b, 0) else b

    def _check_positive_number(self, o: dict[str, Any], key: str, errors: list[str]) -> None:
        try:
            if float(o.get(key, 0)) <= 0:
                errors.append(f"invalid_{key}")
        except Exception:
            errors.append(f"invalid_{key}")

    def _check_non_negative_number(self, o: dict[str, Any], key: str, errors: list[str]) -> None:
        try:
            if float(o.get(key, 0)) < 0:
                errors.append(f"invalid_{key}")
        except Exception:
            errors.append(f"invalid_{key}")

    def _check_float_range(self, o: dict[str, Any], key: str, min_value: float, max_value: float, errors: list[str]) -> None:
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
