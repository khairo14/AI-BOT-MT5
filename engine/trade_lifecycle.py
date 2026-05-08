from __future__ import annotations

"""
Trade lifecycle helpers for EVOTRADE AI.

Responsibilities:
- persistent lifecycle ownership
- TP1 / TP2 state transitions
- BE transitions
- trailing activation state
- close state updates
- lifecycle idempotency checks

This module intentionally does NOT:
- execute orders
- perform RL learning
- write TradeMemory
- recover MT5 history
"""

import logging
from dataclasses import dataclass
from typing import Any

from engine.trade_identity import current_trade_identity
from engine.trade_state import trade_state_store

logger = logging.getLogger(__name__)


@dataclass
class LifecycleState:
    account_mode: str
    account_login: int
    ticket: int
    state: dict[str, Any]


def _merge_event_payload(
    *,
    default_event: str,
    payload: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Merge lifecycle update payload safely.

    Prevents:
        TypeError: got multiple values for keyword argument 'last_event'

    Caller-supplied last_event wins.
    """
    merged: dict[str, Any] = {}

    if payload:
        merged.update(payload)

    if extra:
        merged.update(extra)

    merged.setdefault("last_event", default_event)

    return merged


def get_trade_state(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
) -> LifecycleState:
    """
    Read canonical persistent trade lifecycle state.
    """
    identity = current_trade_identity(source)

    state = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        ticket,
    )

    return LifecycleState(
        account_mode=identity.account_mode,
        account_login=identity.account_login,
        ticket=int(ticket),
        state=state,
    )


def mark_trade_open(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """
    Persist initial open lifecycle state.
    """
    identity = current_trade_identity(source)

    update_payload = _merge_event_payload(
        default_event="opened",
        payload=payload,
    )

    trade_state_store.update(
        identity.account_mode,
        identity.account_login,
        int(ticket),
        account_type=identity.account_type,
        user_id=identity.user_id,
        closed=False,
        tp1_hit=False,
        tp2_hit=False,
        break_even_moved=False,
        trailing_active=False,
        **update_payload,
    )

    logger.info(
        "Lifecycle OPEN initialized %s:%s:%s",
        identity.account_mode,
        identity.account_login,
        ticket,
    )


def mark_tp1_hit(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Mark TP1 partial close exactly once.

    Returns:
        True  -> TP1 was newly marked
        False -> TP1 already processed
    """
    identity = current_trade_identity(source)

    existing = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        int(ticket),
    )

    if existing.get("tp1_hit") is True:
        return False

    update_payload = _merge_event_payload(
        default_event="tp1_hit",
        extra=extra,
    )

    trade_state_store.update(
        identity.account_mode,
        identity.account_login,
        int(ticket),
        tp1_hit=True,
        **update_payload,
    )

    logger.info(
        "Lifecycle TP1 marked %s:%s:%s",
        identity.account_mode,
        identity.account_login,
        ticket,
    )

    return True


def mark_break_even(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Mark break-even movement once.

    IMPORTANT:
    BE movement does NOT imply TP1.
    """
    identity = current_trade_identity(source)

    existing = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        int(ticket),
    )

    if existing.get("break_even_moved") is True:
        return False

    update_payload = _merge_event_payload(
        default_event="break_even",
        extra=extra,
    )

    trade_state_store.update(
        identity.account_mode,
        identity.account_login,
        int(ticket),
        break_even_moved=True,
        **update_payload,
    )

    logger.info(
        "Lifecycle BE marked %s:%s:%s",
        identity.account_mode,
        identity.account_login,
        ticket,
    )

    return True


def mark_trailing_active(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """
    Mark trailing lifecycle activation once.
    """
    identity = current_trade_identity(source)

    existing = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        int(ticket),
    )

    if existing.get("trailing_active") is True:
        return False

    update_payload = _merge_event_payload(
        default_event="trailing_active",
        extra=extra,
    )

    trade_state_store.update(
        identity.account_mode,
        identity.account_login,
        int(ticket),
        trailing_active=True,
        **update_payload,
    )

    logger.info(
        "Lifecycle trailing activated %s:%s:%s",
        identity.account_mode,
        identity.account_login,
        ticket,
    )

    return True


def already_closed(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
) -> bool:
    """
    Idempotent close protection.

    Prevents:
    - restart duplicate close processing
    - recovery replay
    - repeated memory/RL/journal writes
    """
    identity = current_trade_identity(source)

    existing = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        int(ticket),
    )

    return existing.get("closed") is True


def mark_trade_closed(
    ticket: int,
    *,
    source: dict[str, Any] | None = None,
    close_time: str | None = None,
    outcome: str | None = None,
    profit: float | None = None,
    payload: dict[str, Any] | None = None,
) -> bool:
    """
    Persist final closed lifecycle state once.

    Returns:
        True  -> newly closed
        False -> already closed
    """
    identity = current_trade_identity(source)

    existing = trade_state_store.get(
        identity.account_mode,
        identity.account_login,
        int(ticket),
    )

    if existing.get("closed") is True:
        logger.warning(
            "Duplicate close ignored %s:%s:%s",
            identity.account_mode,
            identity.account_login,
            ticket,
        )
        return False

    update_payload = _merge_event_payload(
        default_event="closed",
        payload=payload,
    )

    trade_state_store.update(
        identity.account_mode,
        identity.account_login,
        int(ticket),
        closed=True,
        closed_at=close_time,
        final_outcome=outcome,
        final_profit=profit,
        **update_payload,
    )

    logger.info(
        "Lifecycle CLOSED %s:%s:%s",
        identity.account_mode,
        identity.account_login,
        ticket,
    )

    return True
