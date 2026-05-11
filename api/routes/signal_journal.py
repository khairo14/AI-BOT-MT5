# api/routes/signal_journal.py

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from loguru import logger
from ai.signal_validator import signal_validator

router = APIRouter(prefix="/signal-journal", tags=["signal-journal"])

SIGNAL_JOURNAL_PATH = Path("engine/data/signal_journal.jsonl")


def _read_signal_rows() -> list[dict[str, Any]]:
    if not SIGNAL_JOURNAL_PATH.exists():
        return []

    rows: list[dict[str, Any]] = []

    try:
        with SIGNAL_JOURNAL_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()

                if not line:
                    continue

                try:
                    rows.append(json.loads(line))
                except Exception as exc:
                    logger.warning(f"Signal journal row parse failed: {exc}")

    except Exception as exc:
        logger.error(f"Failed reading signal journal: {exc}")

    return signal_validator.merge_validation_into_signals(rows)


@router.get("/")
def get_signal_journal(
    limit: int = Query(200, ge=1, le=5000),
    symbol: str | None = None,
    strategy: str | None = None,
    decision: str | None = None,
    validated: bool | None = None,
):
    rows = _read_signal_rows()

    filtered: list[dict[str, Any]] = []

    for row in rows:
        if symbol and row.get("symbol_raw") != symbol:
            continue

        if strategy and row.get("strategy") != strategy:
            continue

        if decision and row.get("decision") != decision:
            continue

        if validated is not None:
            if bool(row.get("validated", False)) != validated:
                continue

        filtered.append(row)

    filtered = sorted(
        filtered,
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return {
        "success": True,
        "count": len(filtered),
        "rows": filtered[:limit],
    }


@router.get("/stats")
def get_signal_journal_stats():
    rows = _read_signal_rows()

    decisions = Counter()
    reasons = Counter()
    strategies = Counter()

    validated_wins = 0
    validated_losses = 0
    pending_validation = 0

    for row in rows:
        decisions[row.get("decision", "unknown")] += 1
        reasons[row.get("reason", "unknown")] += 1
        strategies[row.get("strategy", "unknown")] += 1

        validated = bool(row.get("validated", False))

        if not validated:
            pending_validation += 1
            continue

        outcome = str(row.get("validated_outcome", "")).lower()

        if outcome == "win":
            validated_wins += 1
        elif outcome == "loss":
            validated_losses += 1

    return {
        "success": True,
        "total_signals": len(rows),
        "pending_validation": pending_validation,
        "validated_wins": validated_wins,
        "validated_losses": validated_losses,
        "decisions": dict(decisions),
        "reasons": dict(reasons),
        "strategies": dict(strategies),
    }


@router.get("/recent")
def get_recent_signals(limit: int = Query(50, ge=1, le=500)):
    rows = _read_signal_rows()

    rows = sorted(
        rows,
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return {
        "success": True,
        "rows": rows[:limit],
    }


@router.get("/pending-validation")
def get_pending_validation(limit: int = Query(200, ge=1, le=5000)):
    rows = _read_signal_rows()

    pending = [
        r
        for r in rows
        if not bool(r.get("validated", False))
    ]

    pending = sorted(
        pending,
        key=lambda r: r.get("timestamp", ""),
        reverse=True,
    )

    return {
        "success": True,
        "count": len(pending),
        "rows": pending[:limit],
    }

@router.get("/validation-stats")
def get_validation_stats():
    rows = signal_validator.load_validated_results(limit=100000)

    wins = 0
    losses = 0
    no_hit = 0

    by_outcome: dict[str, int] = {}

    for row in rows:
        outcome = str(row.get("validated_outcome", "unknown"))

        by_outcome[outcome] = by_outcome.get(outcome, 0) + 1

        if outcome == "would_tp":
            wins += 1
        elif outcome == "would_sl":
            losses += 1
        else:
            no_hit += 1

    total = len(rows)

    return {
        "success": True,
        "total_validated": total,
        "wins": wins,
        "losses": losses,
        "no_hit": no_hit,
        "winrate": round((wins / total) * 100, 2) if total > 0 else 0.0,
        "outcomes": by_outcome,
    }

@router.post("/run-validation")
def run_signal_validation(
    max_signals: int = Query(100, ge=1, le=2000),
):
    result = signal_validator.validate_pending_signals(
        max_signals=max_signals,
    )

    return {
        "success": True,
        **result,
    }