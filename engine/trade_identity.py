from __future__ import annotations

"""
Trade identity helpers for EVOTRADE AI.

This module centralizes broker/account identity normalization so every runtime
path uses the same language:

    broker execution mode: demo | live

Legacy "paper" is treated as broker demo, not simulation.
There are no simulated paper trades in the current architecture.
"""

from dataclasses import dataclass
from typing import Any, Literal, Mapping

AccountMode = Literal["demo", "live"]

DEMO_ALIASES = {"demo", "paper", "test"}
LIVE_ALIASES = {"live", "real"}


def normalize_account_mode(
    value: Any = None,
    *,
    default: AccountMode = "live",
) -> AccountMode:
    """
    Normalize broker execution mode.

    Runtime supports only:
      - demo
      - live

    Legacy "paper" means broker demo account.
    """
    raw = str(value or default).lower().strip()

    if raw in DEMO_ALIASES:
        return "demo"

    if raw in LIVE_ALIASES:
        return "live"

    return default


def normalize_account_type(
    value: Any = None,
    *,
    fallback_mode: Any = None,
    default: AccountMode = "live",
) -> AccountMode:
    """
    Normalize account_type using the same broker identity rules.

    account_type is display/account identity, while account_mode/execution_mode
    is runtime execution identity. Both must resolve to demo/live.
    """
    if value is not None and str(value).strip():
        return normalize_account_mode(value, default=default)

    return normalize_account_mode(fallback_mode, default=default)


def normalize_execution_mode(
    value: Any = None,
    *,
    default: AccountMode = "live",
) -> AccountMode:
    """
    Normalize execution mode for trade memory, journals, RL, and API filters.
    """
    return normalize_account_mode(value, default=default)


def is_demo_mode(value: Any = None) -> bool:
    return normalize_account_mode(value) == "demo"


def is_live_mode(value: Any = None) -> bool:
    return normalize_account_mode(value) == "live"


def display_account_mode(value: Any = None) -> str:
    """
    User-facing account mode label.
    """
    return "DEMO" if normalize_account_mode(value) == "demo" else "LIVE"


def legacy_filter_mode(value: Any = None) -> str:
    """
    Compatibility helper for older frontend/API filters that still expect paper/live.

    Use this only at API/frontend boundary. Internal backend runtime should use
    normalize_account_mode() and store demo/live.
    """
    return "paper" if normalize_account_mode(value) == "demo" else "live"


def account_login_from(source: Mapping[str, Any] | None = None, *, default: int = 0) -> int:
    """
    Extract account_login safely from a signal, journal row, memory row, or account dict.
    """
    source = source or {}

    raw = (
        source.get("account_login")
        or source.get("login")
        or source.get("account")
    )

    if isinstance(raw, Mapping):
        raw = raw.get("login")

    try:
        return int(raw or default)
    except Exception:
        return int(default)


def account_mode_from(
    source: Mapping[str, Any] | None = None,
    *,
    default: AccountMode = "live",
) -> AccountMode:
    """
    Extract and normalize mode from common persisted/runtime field names.

    Priority:
      1. account_mode
      2. execution_mode
      3. account_type
      4. source
      5. mode, but only if it looks like account mode
    """
    source = source or {}

    raw = (
        source.get("account_mode")
        or source.get("execution_mode")
        or source.get("account_type")
        or source.get("source")
    )

    if raw is None:
        mode_field = str(source.get("mode") or "").lower().strip()
        if mode_field in DEMO_ALIASES or mode_field in LIVE_ALIASES:
            raw = mode_field

    return normalize_account_mode(raw, default=default)


def account_type_from(
    source: Mapping[str, Any] | None = None,
    *,
    default: AccountMode = "live",
) -> AccountMode:
    """
    Extract and normalize account_type from common source fields.
    """
    source = source or {}
    raw = source.get("account_type") or source.get("type")
    return normalize_account_type(
        raw,
        fallback_mode=account_mode_from(source, default=default),
        default=default,
    )


@dataclass(frozen=True)
class TradeIdentity:
    """
    Canonical identity for trade/account-separated runtime operations.

    ticket alone is not globally unique across multiple MT5 accounts.
    Use key for journal/recovery/idempotency decisions.
    """
    account_mode: AccountMode
    account_login: int
    account_type: AccountMode
    user_id: str = "default"

    @property
    def key_prefix(self) -> str:
        return f"{self.account_mode}:{self.account_login}"

    def trade_key(self, ticket: int | str) -> str:
        return f"{self.key_prefix}:{int(ticket or 0)}"


def trade_identity_from(
    source: Mapping[str, Any] | None = None,
    *,
    default_mode: AccountMode = "live",
    default_login: int = 0,
    default_user_id: str = "default",
) -> TradeIdentity:
    """
    Build canonical trade identity from a signal, journal entry, memory row,
    account response, or partial dict.
    """
    source = source or {}

    mode = account_mode_from(source, default=default_mode)
    login = account_login_from(source, default=default_login)
    account_type = account_type_from(source, default=mode)
    user_id = str(source.get("user_id") or default_user_id or "default")

    return TradeIdentity(
        account_mode=mode,
        account_login=login,
        account_type=account_type,
        user_id=user_id,
    )


def current_trade_identity(
    source: Mapping[str, Any] | None = None,
    *,
    default_user_id: str = "default",
) -> TradeIdentity:
    """
    Build identity using source fields first, then engine.account_store.

    Import is intentionally lazy to avoid circular imports during app startup.
    """
    source = dict(source or {})

    try:
        from engine.account_store import current_mode, current_account_login

        default_mode = normalize_account_mode(current_mode())
        default_login = int(current_account_login() or 0)
    except Exception:
        default_mode = "live"
        default_login = 0

    return trade_identity_from(
        source,
        default_mode=default_mode,
        default_login=default_login,
        default_user_id=default_user_id,
    )


def enrich_with_trade_identity(
    payload: dict[str, Any],
    source: Mapping[str, Any] | None = None,
    *,
    default_user_id: str = "default",
) -> dict[str, Any]:
    """
    Mutate and return payload with canonical identity fields.

    This is useful before writing journals/memory/state so all records carry
    consistent account fields.
    """
    identity = current_trade_identity(source or payload, default_user_id=default_user_id)

    payload["account_mode"] = identity.account_mode
    payload["execution_mode"] = identity.account_mode
    payload["source"] = identity.account_mode
    payload["account_login"] = identity.account_login
    payload["account_type"] = identity.account_type
    payload["user_id"] = identity.user_id

    return payload


def same_account(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> bool:
    """
    True when two records/signals belong to the same broker account.
    """
    ia = trade_identity_from(a)
    ib = trade_identity_from(b)

    return ia.account_mode == ib.account_mode and ia.account_login == ib.account_login


def closure_key(source: Mapping[str, Any] | None, ticket: int | str) -> str:
    """
    Stable key for idempotent close/recovery processing.
    """
    return current_trade_identity(source).trade_key(ticket)
