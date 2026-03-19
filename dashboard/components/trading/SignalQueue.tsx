"use client";
import { useState } from "react";
import { approveSignal, rejectSignal } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { Signal, TradingMode } from "@/types";

interface Props {
  mode: TradingMode;
}

const STATUS_BADGE: Record<string, string> = {
  pending:   "bg-yellow-900/50 text-yellow-300 border border-yellow-700",
  executing: "bg-blue-900/50 text-blue-300 border border-blue-700 animate-pulse",
  executed:  "bg-emerald-900/50 text-emerald-300 border border-emerald-700",
  failed:    "bg-red-900/50 text-red-400 border border-red-700",
  approved:  "bg-emerald-900/50 text-emerald-300 border border-emerald-700",
  rejected:  "bg-gray-800 text-gray-500 border border-gray-700",
};

const STATUS_LABEL: Record<string, string> = {
  pending:   "Pending",
  executing: "Executing…",
  executed:  "Executed",
  failed:    "Failed",
  approved:  "Approved",
  rejected:  "Rejected",
};

/** Signals visible in the queue — all non-rejected, non-approved for this mode */
const VISIBLE_STATUSES = new Set(["pending", "executing", "executed", "failed"]);

export default function SignalQueue({ mode }: Props) {
  const { signals, updateSignalStatus, pushNotification } = useBotStore();
  const [rejectReason, setRejectReason] = useState<Record<string, string>>({});

  const visible = signals.filter(
    (s) => s.mode === mode && VISIBLE_STATUSES.has(s.status)
  );

  const handleApprove = async (s: Signal) => {
    try {
      await approveSignal(s.id);
      updateSignalStatus(s.id, "approved");
      pushNotification({
        type: "success",
        title: "Signal approved",
        message: `${s.direction.toUpperCase()} ${s.symbol} — executing…`,
      });
    } catch {
      pushNotification({ type: "error", title: "Approve failed", message: s.id });
    }
  };

  const handleReject = async (s: Signal) => {
    try {
      await rejectSignal(s.id, rejectReason[s.id]);
      updateSignalStatus(s.id, "rejected");
    } catch {
      pushNotification({ type: "error", title: "Reject failed", message: s.id });
    }
  };

  if (!visible.length) {
    return (
      <p className="text-gray-600 text-sm text-center py-4">
        No signals for {mode.replace("_", " ")}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      {visible.map((s) => (
        <div
          key={s.id}
          className="bg-gray-800/60 border border-gray-700 rounded-xl p-4 space-y-3"
        >
          {/* Header */}
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span
                className={`text-xs font-bold px-2 py-0.5 rounded-full ${
                  s.direction === "buy"
                    ? "bg-emerald-900 text-emerald-300"
                    : "bg-red-900 text-red-300"
                }`}
              >
                {s.direction.toUpperCase()}
              </span>
              <span className="font-semibold text-white">{s.symbol}</span>
              <span className="text-gray-500 text-xs">{s.strategy}</span>
            </div>
            <div className="flex items-center gap-2">
              <span
                className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                  s.confidence >= 0.75
                    ? "bg-emerald-900/50 text-emerald-400"
                    : s.confidence >= 0.5
                    ? "bg-yellow-900/50 text-yellow-400"
                    : "bg-gray-700 text-gray-400"
                }`}
              >
                {(s.confidence * 100).toFixed(0)}% conf
              </span>
              <span className={`text-xs px-2 py-0.5 rounded-full ${STATUS_BADGE[s.status] ?? ""}`}>
                {STATUS_LABEL[s.status] ?? s.status}
              </span>
            </div>
          </div>

          {/* Levels */}
          <div className="grid grid-cols-3 gap-2 text-xs">
            <div className="bg-gray-900 rounded px-2 py-1.5">
              <p className="text-gray-500">Entry</p>
              <p className="text-white font-mono">{s.entry?.toFixed(5) ?? "—"}</p>
            </div>
            <div className="bg-gray-900 rounded px-2 py-1.5">
              <p className="text-red-400">SL</p>
              <p className="text-white font-mono">{s.sl?.toFixed(5) ?? "—"}</p>
            </div>
            <div className="bg-gray-900 rounded px-2 py-1.5">
              <p className="text-emerald-400">TP</p>
              <p className="text-white font-mono">{s.tp?.toFixed(5) ?? "—"}</p>
            </div>
          </div>

          {/* Rejection reason */}
          {(s.status === "failed" || s.status === "rejected") &&
            (s.rejection_reason || s.reason) && (
            <p className="text-xs text-red-400 bg-red-950/40 rounded px-2 py-1.5 border border-red-900/40">
              {s.rejection_reason ?? s.reason}
            </p>
          )}

          {/* Actions — only for pending signals */}
          {s.status === "pending" && (
            <div className="flex gap-2">
              <button
                onClick={() => handleApprove(s)}
                className="flex-1 py-1.5 text-xs rounded-lg bg-emerald-700 hover:bg-emerald-600 text-white font-semibold transition-colors"
              >
                ✓ Approve
              </button>
              <input
                type="text"
                placeholder="Reason (optional)"
                value={rejectReason[s.id] ?? ""}
                onChange={(e) =>
                  setRejectReason((r) => ({ ...r, [s.id]: e.target.value }))
                }
                className="flex-1 bg-gray-900 border border-gray-700 rounded-lg px-2 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-gray-500"
              />
              <button
                onClick={() => handleReject(s)}
                className="flex-1 py-1.5 text-xs rounded-lg bg-red-900 hover:bg-red-800 text-red-300 font-semibold transition-colors"
              >
                ✗ Reject
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
