"""
Notification Manager
====================
In-app notification system for important trading events.

Notifications are stored in-memory with a configurable max size (default 100).
Older notifications are automatically pruned when limit is reached.

Notification Types:
- signal_generated: New trading signal created
- position_opened: Position successfully opened
- position_closed: Position closed (TP/SL/manual)
- circuit_breaker: Circuit breaker activated/deactivated
- model_trained: LSTM model retrained
- optimizer_complete: Parameter optimization finished
- risk_alert: Risk thresholds exceeded
- regime_change: Market regime transition detected
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Literal, Optional

from loguru import logger
import json
import requests
from pathlib import Path

NotificationType = Literal[
    "signal_generated",
    "position_opened", 
    "position_closed",
    "circuit_breaker",
    "model_trained",
    "optimizer_complete",
    "risk_alert",
    "regime_change",
]


@dataclass
class Notification:
    """Single notification entry."""
    id: str
    type: NotificationType
    title: str
    message: str
    severity: Literal["info", "success", "warning", "error"]
    timestamp: str  # ISO 8601
    read: bool = False
    metadata: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

_APP_CFG_CACHE: dict[str, object] | None = None


def _load_app_cfg() -> dict[str, object]:
    global _APP_CFG_CACHE

    if isinstance(_APP_CFG_CACHE, dict):
        return _APP_CFG_CACHE

    cfg: dict[str, object] = {}

    try:
        cfg_path = Path("config/app.json")

        if cfg_path.exists():
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)

            if isinstance(loaded, dict):
                cfg = loaded
    except Exception:
        cfg = {}

    _APP_CFG_CACHE = cfg
    return cfg

def _send_ntfy(
    *,
    title: str,
    message: str,
    severity: str,
    type: str,
) -> None:
    try:
        cfg = _load_app_cfg()

        notifications_cfg = cfg.get("notifications", {})
        if not isinstance(notifications_cfg, dict):
            return

        ntfy_cfg = notifications_cfg.get("ntfy", {})
        if not isinstance(ntfy_cfg, dict):
            return

        allowed_types = ntfy_cfg.get("send_types", [])

        if type not in allowed_types:
            return

        priority_map = {
            "info": "default",
            "success": "default",
            "warning": "high",
            "error": "urgent",
        }

        min_priority = ntfy_cfg.get("priority_min", "warning")

        severity_rank = {
            "info": 0,
            "success": 1,
            "warning": 2,
            "error": 3,
        }

        if severity_rank.get(severity, 0) < severity_rank.get(min_priority, 2):
            return

        server = str(ntfy_cfg.get("server", "https://ntfy.sh")).rstrip("/")
        topic = ntfy_cfg.get("topic", "evotrade-live")

        requests.post(
            f"{server}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority_map.get(severity, "default"),
                "Tags": "money_bag",
            },
            timeout=5,
        )

    except Exception as exc:
        logger.debug(f"ntfy send failed: {exc}")


class NotificationManager:
    """
    Thread-safe in-memory notification manager.
    
    Stores last N notifications (default 100) in a deque.
    Provides mark_read, mark_all_read, delete, and query by type/read status.
    """

    def __init__(self, max_size: int = 100):
        self._notifications: deque[Notification] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._next_id = 1

    def add(
        self,
        type: NotificationType,
        title: str,
        message: str,
        severity: Literal["info", "success", "warning", "error"] = "info",
        metadata: Optional[dict] = None,
    ) -> Notification:
        """
        Add a new notification. Thread-safe.
        Returns the created notification object.
        """
        with self._lock:
            notification = Notification(
                id=str(self._next_id),
                type=type,
                title=title,
                message=message,
                severity=severity,
                timestamp=datetime.now(timezone.utc).isoformat(),
                read=False,
                metadata=metadata or {},
            )
            self._notifications.append(notification)
            self._next_id += 1
            logger.debug(f"Notification added: {type} | {title}")

            try:
                _send_ntfy(
                    title=title,
                    message=message,
                    severity=severity,
                    type=type,
                )
            except Exception:
                pass

            return notification

    def get_all(self, unread_only: bool = False, type_filter: Optional[NotificationType] = None) -> list[dict]:
        """
        Get all notifications, newest first.
        
        Args:
            unread_only: If True, return only unread notifications
            type_filter: If specified, return only notifications of this type
        """
        with self._lock:
            notifications = list(self._notifications)
        
        # Filter
        if unread_only:
            notifications = [n for n in notifications if not n.read]
        if type_filter:
            notifications = [n for n in notifications if n.type == type_filter]
        
        # Reverse to get newest first
        notifications.reverse()
        return [n.to_dict() for n in notifications]

    def get_by_id(self, notification_id: str) -> Optional[dict]:
        """Get a single notification by ID."""
        with self._lock:
            for notification in self._notifications:
                if notification.id == notification_id:
                    return notification.to_dict()
        return None

    def mark_read(self, notification_id: str) -> bool:
        """Mark a notification as read. Returns True if found."""
        with self._lock:
            for notification in self._notifications:
                if notification.id == notification_id:
                    notification.read = True
                    return True
        return False

    def mark_all_read(self) -> int:
        """Mark all notifications as read. Returns count of updated notifications."""
        with self._lock:
            count = 0
            for notification in self._notifications:
                if not notification.read:
                    notification.read = True
                    count += 1
            return count

    def delete(self, notification_id: str) -> bool:
        """Delete a notification by ID. Returns True if found and deleted."""
        with self._lock:
            for i, notification in enumerate(self._notifications):
                if notification.id == notification_id:
                    del self._notifications[i]
                    return True
        return False

    def clear_all(self) -> int:
        """Clear all notifications. Returns count of deleted notifications."""
        with self._lock:
            count = len(self._notifications)
            self._notifications.clear()
            self._next_id = 1
            return count

    def unread_count(self) -> int:
        """Return count of unread notifications."""
        with self._lock:
            return sum(1 for n in self._notifications if not n.read)

    def stats(self) -> dict:
        """Return notification statistics."""
        with self._lock:
            total = len(self._notifications)
            unread = sum(1 for n in self._notifications if not n.read)
            by_type = {}
            by_severity = {}
            for n in self._notifications:
                by_type[n.type] = by_type.get(n.type, 0) + 1
                by_severity[n.severity] = by_severity.get(n.severity, 0) + 1
            
            return {
                "total": total,
                "unread": unread,
                "read": total - unread,
                "by_type": by_type,
                "by_severity": by_severity,
                "capacity": self._notifications.maxlen,
            }


# Global singleton
notification_manager = NotificationManager(max_size=100)
