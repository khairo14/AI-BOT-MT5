"use client";
import { useBotStore } from "@/lib/store";

const TYPE_STYLES: Record<string, string> = {
  error:   "border-red-700 bg-red-950/40 text-red-300",
  warning: "border-yellow-700 bg-yellow-950/40 text-yellow-300",
  success: "border-emerald-700 bg-emerald-950/40 text-emerald-300",
  info:    "border-gray-700 bg-gray-900 text-gray-300",
};

const TYPE_ICON: Record<string, string> = {
  error:   "✕",
  warning: "⚠",
  success: "✓",
  info:    "ℹ",
};

export default function NotificationsPage() {
  const { notifications, dismissNotification } = useBotStore();

  return (
    <div className="p-8 max-w-3xl space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">Notifications</h2>
          <p className="text-gray-500 text-sm mt-1">
            {notifications.length > 0
              ? `${notifications.length} notification${notifications.length !== 1 ? "s" : ""} this session`
              : "No notifications yet"}
          </p>
        </div>
        {notifications.length > 0 && (
          <button
            onClick={() => notifications.forEach((n) => dismissNotification(n.id))}
            className="text-xs text-gray-500 hover:text-white transition-colors px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700"
          >
            Clear all
          </button>
        )}
      </div>

      {/* Empty state */}
      {notifications.length === 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-10 text-center">
          <p className="text-4xl mb-3">🔔</p>
          <p className="text-gray-500 text-sm">
            Notifications will appear here when signals fire, orders fill, or alerts trigger.
          </p>
        </div>
      )}

      {/* Notification list */}
      <div className="space-y-2">
        {notifications.map((n) => (
          <div
            key={n.id}
            className={`flex items-start gap-4 border rounded-xl px-5 py-4 ${TYPE_STYLES[n.type] ?? TYPE_STYLES.info}`}
          >
            <span className="text-lg font-bold shrink-0 mt-0.5">
              {TYPE_ICON[n.type] ?? "ℹ"}
            </span>
            <div className="flex-1 min-w-0">
              <p className="font-semibold text-sm">{n.title}</p>
              {n.message && (
                <p className="text-xs opacity-75 mt-0.5">{n.message}</p>
              )}
              <p className="text-xs opacity-40 mt-1">
                {new Date(n.timestamp).toLocaleTimeString()}
              </p>
            </div>
            <button
              onClick={() => dismissNotification(n.id)}
              className="shrink-0 opacity-40 hover:opacity-100 text-lg leading-none"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
