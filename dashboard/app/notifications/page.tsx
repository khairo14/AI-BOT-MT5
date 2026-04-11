"use client";
import { useEffect, useState } from "react";
import {
  fetchNotifications,
  markNotificationRead,
  markAllNotificationsRead,
  deleteNotification,
  clearAllNotifications,
} from "@/lib/api";
import type { Notification, NotificationSeverity, NotificationType } from "@/types";

const SEVERITY_STYLES: Record<NotificationSeverity, string> = {
  info: "border-gray-700 bg-gray-900 text-gray-300",
  success: "border-emerald-700 bg-emerald-950/40 text-emerald-300",
  warning: "border-yellow-700 bg-yellow-950/40 text-yellow-300",
  error: "border-red-700 bg-red-950/40 text-red-300",
};

const TYPE_ICONS: Record<NotificationType, string> = {
  signal_generated: "📊",
  position_opened: "✅",
  position_closed: "🔐",
  circuit_breaker: "⚠️",
  model_trained: "🤖",
  optimizer_complete: "⚙️",
  risk_alert: "🚨",
  regime_change: "🔄",
};

export default function NotificationsPage() {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [filter, setFilter] = useState<"all" | "unread">("all");
  const [loading, setLoading] = useState(true);

  const loadNotifications = async () => {
    try {
      const data = await fetchNotifications(filter === "unread");
      setNotifications(data);
    } catch (error) {
      console.error("Failed to load notifications:", error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadNotifications();
    const interval = setInterval(loadNotifications, 10000);
    return () => clearInterval(interval);
  }, [filter]);

  const handleMarkRead = async (id: string) => {
    try {
      await markNotificationRead(id);
      setNotifications((prev) =>
        prev.map((n) => (n.id === id ? { ...n, read: true } : n))
      );
    } catch (error) {
      console.error("Failed to mark as read:", error);
    }
  };

  const handleMarkAllRead = async () => {
    try {
      await markAllNotificationsRead();
      setNotifications((prev) => prev.map((n) => ({ ...n, read: true })));
    } catch (error) {
      console.error("Failed to mark all as read:", error);
    }
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteNotification(id);
      setNotifications((prev) => prev.filter((n) => n.id !== id));
    } catch (error) {
      console.error("Failed to delete notification:", error);
    }
  };

  const handleClearAll = async () => {
    if (!confirm("Clear all notifications?")) return;
    try {
      await clearAllNotifications();
      setNotifications([]);
    } catch (error) {
      console.error("Failed to clear all:", error);
    }
  };

  const formatTime = (timestamp: string) => {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMin = Math.floor(diffMs / 60000);
    const diffHour = Math.floor(diffMs / 3600000);
    const diffDay = Math.floor(diffMs / 86400000);

    if (diffMin < 1) return "Just now";
    if (diffMin < 60) return `${diffMin}m ago`;
    if (diffHour < 24) return `${diffHour}h ago`;
    if (diffDay < 7) return `${diffDay}d ago`;
    return date.toLocaleDateString() + " " + date.toLocaleTimeString();
  };

  const unreadCount = notifications.filter((n) => !n.read).length;

  return (
    <div className="p-8 max-w-4xl space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Notifications</h2>
          <p className="text-gray-500 text-sm mt-1">
            {notifications.length > 0
              ? `${notifications.length} notification${notifications.length !== 1 ? "s" : ""} (${unreadCount} unread)`
              : "No notifications yet"}
          </p>
        </div>
        <div className="flex gap-2">
          {unreadCount > 0 && (
            <button
              onClick={handleMarkAllRead}
              className="text-xs text-gray-400 hover:text-white transition-colors px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700"
            >
              Mark all read
            </button>
          )}
          {notifications.length > 0 && (
            <button
              onClick={handleClearAll}
              className="text-xs text-red-400 hover:text-red-300 transition-colors px-3 py-1.5 rounded-lg bg-red-950/50 hover:bg-red-900/50"
            >
              Clear all
            </button>
          )}
        </div>
      </div>

      {/* Filter tabs */}
      <div className="flex gap-2 border-b border-gray-800">
        <button
          onClick={() => setFilter("all")}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            filter === "all"
              ? "text-blue-400 border-b-2 border-blue-500"
              : "text-gray-400 hover:text-gray-200"
          }`}
        >
          All ({notifications.length})
        </button>
        <button
          onClick={() => setFilter("unread")}
          className={`px-4 py-2 text-sm font-medium transition-colors ${
            filter === "unread"
              ? "text-blue-400 border-b-2 border-blue-500"
              : "text-gray-400 hover:text-gray-200"
          }`}
        >
          Unread ({unreadCount})
        </button>
      </div>

      {/* Loading state */}
      {loading && notifications.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center">
          <p className="text-gray-500 text-sm">Loading notifications...</p>
        </div>
      ) : null}

      {/* Empty state */}
      {!loading && notifications.length === 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center">
          <p className="text-4xl mb-3">🔔</p>
          <p className="text-gray-500 text-sm">
            {filter === "unread"
              ? "No unread notifications"
              : "Notifications will appear here when signals fire, models train, or alerts trigger."}
          </p>
        </div>
      )}

      {/* Notification list */}
      <div className="space-y-2">
        {notifications.map((n) => (
          <div
            key={n.id}
            className={`flex items-start gap-4 border rounded-xl px-5 py-4 ${
              SEVERITY_STYLES[n.severity]
            } ${!n.read ? "ring-2 ring-blue-500/30" : ""}`}
          >
            <span className="text-2xl shrink-0 mt-0.5">
              {TYPE_ICONS[n.type] || "📢"}
            </span>
            <div className="flex-1 min-w-0">
              <div className="flex items-start justify-between gap-2 mb-1">
                <p className="font-semibold text-sm">{n.title}</p>
                <span
                  className={`shrink-0 px-2 py-0.5 text-xs rounded border ${
                    n.severity === "info"
                      ? "bg-blue-900/30 border-blue-700 text-blue-300"
                      : n.severity === "success"
                      ? "bg-emerald-900/30 border-emerald-700 text-emerald-300"
                      : n.severity === "warning"
                      ? "bg-yellow-900/30 border-yellow-700 text-yellow-300"
                      : "bg-red-900/30 border-red-700 text-red-300"
                  }`}
                >
                  {n.severity}
                </span>
              </div>
              <p className="text-xs opacity-75 mb-2">{n.message}</p>
              <div className="flex items-center justify-between text-xs">
                <span className="opacity-50">{formatTime(n.timestamp)}</span>
                <div className="flex gap-2">
                  {!n.read && (
                    <button
                      onClick={() => handleMarkRead(n.id)}
                      className="text-blue-400 hover:text-blue-300"
                    >
                      Mark read
                    </button>
                  )}
                  <button
                    onClick={() => handleDelete(n.id)}
                    className="text-red-400 hover:text-red-300"
                  >
                    Delete
                  </button>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
