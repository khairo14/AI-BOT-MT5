from __future__ import annotations

"""
Trade learning orchestration for EVOTRADE AI.

Responsibilities:
- Validate learning eligibility
- Feed TradeMemory
- Feed RL only once per valid close
- Prevent duplicate close learning
- Keep learning side-effects isolated from lifecycle polling

This module does NOT:
- manage TP/BE/trailing
- execute orders
- recover positions
"""

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai.rl_agent import rl_manager
from ai.trade_memory import TradeOutcome, memory
from engine.trade_identity import closure_key

logger = logging.getLogger(__name__)

PROCESSED_CLOSURES_FILE = Path("data/processed_trade_closures.json")
_lock = threading.Lock()


@dataclass
class LearningResult:
    outcome: TradeOutcome
    recorded: dict[str, Any]
    duplicate_close: bool
    rl_applied: bool


def _load_processed() -> dict[str, str]:
    try:
        if PROCESSED_CLOSURES_FILE.exists():
            return json.loads(PROCESSED_CLOSURES_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("Failed loading processed closures: %s", exc)

    return {}


def _save_processed(data: dict[str, str]) -> None:
    PROCESSED_CLOSURES_FILE.parent.mkdir(parents=True, exist_ok=True)

    tmp = PROCESSED_CLOSURES_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(PROCESSED_CLOSURES_FILE)


def _processed_key(outcome: TradeOutcome) -> str:
    return closure_key(
        {
            "account_mode": outcome.execution_mode or outcome.source,
            "account_login": outcome.account_login,
        },
        outcome.ticket,
    )


def mark_close_processed_once(outcome: TradeOutcome) -> bool:
    """
    Mark this account+ticket close as processed.

    Returns:
        True  -> newly marked
        False -> already processed

    Marking happens before side effects so restart/recovery races cannot replay
    memory/RL learning for the same broker close.
    """
    key = _processed_key(outcome)

    with _lock:
        data = _load_processed()

        if key in data:
            return False

        data[key] = datetime.now(timezone.utc).isoformat()
        _save_processed(data)

    return True


def already_processed_close(outcome: TradeOutcome) -> bool:
    key = _processed_key(outcome)

    with _lock:
        data = _load_processed()
        return key in data


def apply_trade_learning(
    outcome: TradeOutcome,
    *,
    win_rate: float = 0.5,
    avg_conf: float = 0.5,
    drawdown_pct: float = 0.0,
    vol_pct: float = 0.0,
) -> LearningResult:
    """
    Centralized learning pipeline.

    Rules:
    - every close can be recorded to TradeMemory once
    - RL only learns from learning_valid=True
    - duplicate recovered closes are ignored entirely
    """
    if not mark_close_processed_once(outcome):
        logger.warning(
            "Duplicate close learning ignored: %s:%s",
            outcome.account_login,
            outcome.ticket,
        )

        return LearningResult(
            outcome=outcome,
            recorded={},
            duplicate_close=True,
            rl_applied=False,
        )

    recorded = memory.record(outcome)
    learning_valid = recorded.get("learning_valid") is True
    rl_applied = False

    if learning_valid:
        try:
            rl_manager.on_trade_closed(
                trading_type=outcome.trading_type,
                profit_pct=float(outcome.profit_pct or 0.0),
                win_rate=float(win_rate),
                avg_conf=float(avg_conf),
                drawdown_pct=float(drawdown_pct),
                vol_pct=float(vol_pct),
                strategy_name=outcome.strategy or None,
            )
            rl_applied = True

        except Exception as exc:
            logger.exception(
                "RL learning failed for ticket=%s: %s",
                outcome.ticket,
                exc,
            )

    else:
        logger.warning(
            "Skipping RL learning for invalid trade outcome "
            "ticket=%s validation_errors=%s",
            outcome.ticket,
            recorded.get("validation_errors"),
        )

    return LearningResult(
        outcome=outcome,
        recorded=recorded,
        duplicate_close=False,
        rl_applied=rl_applied,
    )
