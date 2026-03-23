"use client";
import { useBotStore } from "@/lib/store";

export default function NotificationBar() {
  const { notifications, dismissNotification } = useBotStore();
  if (!notifications.length) return null;

  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm w-full pointer-events-none">
      {notifications.slice(0, 5).map((n) => (
        <div
          key={n.id}
          className={`pointer-events-auto flex items-start gap-3 rounded-lg px-4 py-3 shadow-lg text-sm border ${
            n.type === "error"
              ? "bg-red-950 border-red-700 text-red-200"
              : n.type === "warning"
              ? "bg-yellow-950 border-yellow-700 text-yellow-200"
              : n.type === "success"
              ? "bg-emerald-950 border-emerald-700 text-emerald-200"
              : "bg-gray-900 border-gray-700 text-gray-200"
          }`}
        >
          <div className="flex-1 min-w-0">
            <p className="font-semibold truncate">{n.title}</p>
            <p className="text-xs opacity-75 mt-0.5">{typeof n.message === "string" ? n.message : JSON.stringify(n.message)}</p>
          </div>
          <button
            onClick={() => dismissNotification(n.id)}
            className="shrink-0 opacity-50 hover:opacity-100 text-lg leading-none"
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
