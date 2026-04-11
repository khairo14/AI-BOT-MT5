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
import time
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Literal, Optional

from loguru import logger

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
