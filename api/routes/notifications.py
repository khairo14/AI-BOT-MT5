"""
Notification API Endpoints
===========================
In-app notifications for trading events.
"""

from fastapi import APIRouter, HTTPException
from typing import Optional, Literal

from engine.notification_manager import notification_manager, NotificationType

router = APIRouter()


@router.get("")
def get_notifications(
    unread_only: bool = False,
    type: Optional[NotificationType] = None,
):
    """
    Get all notifications, newest first.
    
    Query parameters:
    - unread_only: If true, return only unread notifications
    - type: Filter by notification type
    """
    return {
        "notifications": notification_manager.get_all(unread_only=unread_only, type_filter=type),
        "unread_count": notification_manager.unread_count(),
    }


@router.get("/stats")
def get_notification_stats():
    """Get notification statistics."""
    return notification_manager.stats()


@router.get("/{notification_id}")
def get_notification(notification_id: str):
    """Get a single notification by ID."""
    notification = notification_manager.get_by_id(notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail=f"Notification {notification_id} not found")
    return notification


@router.post("/{notification_id}/read")
def mark_notification_read(notification_id: str):
    """Mark a notification as read."""
    success = notification_manager.mark_read(notification_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Notification {notification_id} not found")
    return {"status": "marked_read", "id": notification_id}


@router.post("/mark-all-read")
def mark_all_notifications_read():
    """Mark all notifications as read."""
    count = notification_manager.mark_all_read()
    return {"status": "all_marked_read", "count": count}


@router.delete("/{notification_id}")
def delete_notification(notification_id: str):
    """Delete a notification."""
    success = notification_manager.delete(notification_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"Notification {notification_id} not found")
    return {"status": "deleted", "id": notification_id}


@router.delete("")
def clear_all_notifications():
    """Clear all notifications."""
    count = notification_manager.clear_all()
    return {"status": "cleared", "count": count}


@router.post("/test")
def create_test_notification(
    type: NotificationType = "signal_generated",
    severity: Literal["info", "success", "warning", "error"] = "info",
):
    """
    Create a test notification (for development/testing).
    """
    titles = {
        "signal_generated": "Test Signal Generated",
        "position_opened": "Test Position Opened",
        "position_closed": "Test Position Closed",
        "circuit_breaker": "Test Circuit Breaker",
        "model_trained": "Test Model Trained",
        "optimizer_complete": "Test Optimizer Complete",
        "risk_alert": "Test Risk Alert",
        "regime_change": "Test Regime Change",
    }
    messages = {
        "signal_generated": "EURUSD BUY signal generated with 85% confidence",
        "position_opened": "EURUSD BUY #12345 opened at 1.08500",
        "position_closed": "EURUSD BUY #12345 closed at 1.08750 (+25 pips, +$250)",
        "circuit_breaker": "Circuit breaker activated: 3 consecutive losses",
        "model_trained": "EURUSD day_trading model retrained (65% accuracy)",
        "optimizer_complete": "ema_scalp optimizer finished for GBPUSD",
        "risk_alert": "Daily loss limit 80% reached (-$400 of -$500 max)",
        "regime_change": "EURUSD regime changed: trending_bull → ranging_low_vol",
    }
    
    notification = notification_manager.add(
        type=type,
        title=titles[type],
        message=messages[type],
        severity=severity,
        metadata={"test": True, "symbol": "EURUSD"},
    )
    return notification.to_dict()
