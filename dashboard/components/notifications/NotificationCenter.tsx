"use client";
import { useEffect, useState } from "react";
import {
  fetchNotifications,
  markNotificationRead,
  markAllNotificationsRead,
  deleteNotification,
  clearAllNotifications,
  getNotificationStats,
} from "@/lib/api";
import type { Notification, NotificationSeverity, NotificationType } from "@/types";

const SEVERITY_STYLES: Record<NotificationSeverity, string> = {
  info: "bg-blue-900/30 border-blue-700 text-blue-200",
  success: "bg-emerald-900/30 border-emerald-700 text-emerald-200",
  warning: "bg-yellow-900/30 border-yellow-700 text-yellow-200",
  error: "bg-red-900/30 border-red-700 text-red-200",
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

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

export default function NotificationCenter({ isOpen, onClose }: Props) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [filter, setFilter] = useState<"all" | "unread">("all");
  const [loading, setLoading] = useState(false);

  const loadNotifications = async () => {
    setLoading(true);
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
    if (isOpen) {
      loadNotifications();
      const interval = setInterval(loadNotifications, 10000); // Refresh every 10s
      return () => clearInterval(interval);
    }
  }, [isOpen, filter]);

  const handleMarkRead = async (id: string) => {
    try {
      await markNotificationRead(id);
      setNotifications((prev) =>
        prev.map((n) => (n.id === id ? { ...n, read: true } : n))
      );
    } catch (error) {
      console.error("Failed to mark notification as read:", error);
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
      console.error("Failed to clear all notifications:", error);
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
    return date.toLocaleDateString();
  };

  if (!isOpen) return null;

  const unreadCount = notifications.filter((n) => !n.read).length;

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 z-40"
        onClick={onClose}
      />

      {/* Notification Panel */}
      <div className="fixed right-0 top-0 h-full w-full max-w-md bg-gray-950 border-l border-gray-800 z-50 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="border-b border-gray-800 p-4 flex items-center justify-between bg-gray-900/50">
          <div>
            <h2 className="text-lg font-semibold text-gray-100">Notifications</h2>
            {unreadCount > 0 && (
              <p className="text-xs text-gray-400">{unreadCount} unread</p>
            )}
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-200 text-2xl leading-none"
          >
            ×
          </button>
        </div>

        {/* Filter Tabs */}
        <div className="border-b border-gray-800 flex bg-gray-900/30">
          <button
            onClick={() => setFilter("all")}
            className={`flex-1 py-2 text-sm font-medium transition-colors ${
              filter === "all"
                ? "text-blue-400 border-b-2 border-blue-500"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            All ({notifications.length})
          </button>
          <button
            onClick={() => setFilter("unread")}
            className={`flex-1 py-2 text-sm font-medium transition-colors ${
              filter === "unread"
                ? "text-blue-400 border-b-2 border-blue-500"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            Unread ({unreadCount})
          </button>
        </div>

        {/* Actions */}
        <div className="p-2 border-b border-gray-800 flex gap-2">
          <button
            onClick={handleMarkAllRead}
            disabled={unreadCount === 0}
            className="text-xs px-3 py-1 bg-blue-900/30 hover:bg-blue-900/50 text-blue-300 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Mark all read
          </button>
          <button
            onClick={handleClearAll}
            disabled={notifications.length === 0}
            className="text-xs px-3 py-1 bg-red-900/30 hover:bg-red-900/50 text-red-300 rounded disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            Clear all
          </button>
        </div>

        {/* Notification List */}
        <div className="flex-1 overflow-y-auto">
          {loading && notifications.length === 0 ? (
            <div className="text-center text-gray-500 py-8">Loading...</div>
          ) : notifications.length === 0 ? (
            <div className="text-center text-gray-500 py-8">
              {filter === "unread" ? "No unread notifications" : "No notifications"}
            </div>
          ) : (
            <div className="divide-y divide-gray-800">
              {notifications.map((notification) => (
                <div
                  key={notification.id}
                  className={`p-4 hover:bg-gray-900/50 transition-colors ${
                    !notification.read ? "bg-blue-950/10" : ""
                  }`}
                >
                  <div className="flex items-start gap-3">
                    {/* Icon */}
                    <div className="text-2xl shrink-0 mt-0.5">
                      {TYPE_ICONS[notification.type] || "📢"}
                    </div>

                    {/* Content */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-start justify-between gap-2 mb-1">
                        <h3 className="font-semibold text-sm text-gray-100 truncate">
                          {notification.title}
                        </h3>
                        <span
                          className={`shrink-0 px-2 py-0.5 text-xs rounded border ${
                            SEVERITY_STYLES[notification.severity]
                          }`}
                        >
                          {notification.severity}
                        </span>
                      </div>
                      <p className="text-sm text-gray-400 mb-2">{notification.message}</p>
                      <div className="flex items-center justify-between">
                        <span className="text-xs text-gray-500">
                          {formatTime(notification.timestamp)}
                        </span>
                        <div className="flex gap-2">
                          {!notification.read && (
                            <button
                              onClick={() => handleMarkRead(notification.id)}
                              className="text-xs text-blue-400 hover:text-blue-300"
                            >
                              Mark read
                            </button>
                          )}
                          <button
                            onClick={() => handleDelete(notification.id)}
                            className="text-xs text-red-400 hover:text-red-300"
                          >
                            Delete
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
