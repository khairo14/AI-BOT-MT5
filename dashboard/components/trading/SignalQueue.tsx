"use client";
import { useState, useEffect, useRef } from "react";
import { approveSignal, rejectSignal } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { Signal, TradingMode } from "@/types";

/** Countdown badge shown on pending signals that have an expires_at timestamp. */
function ExpiryCountdown({ expiresAt }: { expiresAt: string }) {
  const computeSecsLeft = () => {
    const diff = Math.floor(
      (new Date(expiresAt).getTime() - Date.now()) / 1000
    );
    return Math.max(diff, 0);
  };

  const [secsLeft, setSecsLeft] = useState(computeSecsLeft);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    intervalRef.current = setInterval(() => {
      const left = computeSecsLeft();
      setSecsLeft(left);
      if (left <= 0 && intervalRef.current) {
        clearInterval(intervalRef.current);
      }
    }, 1000);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expiresAt]);

  if (secsLeft <= 0) {
    return (
      <span className="text-xs font-bold px-2 py-0.5 rounded-full bg-gray-800 text-gray-500 border border-gray-700">
        EXPIRED
      </span>
    );
  }

  const mins = Math.floor(secsLeft / 60);
  const secs = secsLeft % 60;
  const label = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;

  const isUrgent = secsLeft < 60;
  return (
    <span
      className={`text-xs font-semibold px-2 py-0.5 rounded-full border ${
        isUrgent
          ? "bg-red-950/60 text-red-400 border-red-700 animate-pulse"
          : "bg-yellow-950/40 text-yellow-400 border-yellow-800"
      }`}
      title="Signal expires after this time — market conditions may change"
    >
      ⏱ {label}
    </span>
  );
}

interface PendingActionsProps {
  s: Signal;
  rejectReason: Record<string, string>;
  setRejectReason: React.Dispatch<React.SetStateAction<Record<string, string>>>;
  handleApprove: (s: Signal) => void;
  handleReject: (s: Signal) => void;
}

/** Approve/reject row for pending signals. Disables approve when signal is past its expiry. */
function PendingActions({ s, rejectReason, setRejectReason, handleApprove, handleReject }: PendingActionsProps) {
  const [isLocallyExpired, setIsLocallyExpired] = useState(false);

  useEffect(() => {
    if (!s.expires_at) return;
    const checkExpiry = () => {
      if (new Date(s.expires_at!).getTime() <= Date.now()) {
        setIsLocallyExpired(true);
      }
    };
    checkExpiry();
    const interval = setInterval(checkExpiry, 500);
    return () => clearInterval(interval);
  }, [s.expires_at]);

  if (isLocallyExpired) {
    return (
      <p className="text-xs text-gray-500 text-center py-1">
        Signal has expired — approval blocked
      </p>
    );
  }

  return (
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
  );
}

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
  expired:   "bg-gray-800 text-gray-500 border border-gray-700",
};

const STATUS_LABEL: Record<string, string> = {
  pending:   "Pending",
  executing: "Executing…",
  executed:  "Executed",
  failed:    "Failed",
  approved:  "Approved",
  rejected:  "Rejected",
  expired:   "Expired",
};

/** Signals visible in the queue — pending / in-flight / completed / expired */
const VISIBLE_STATUSES = new Set(["pending", "executing", "executed", "failed", "expired"]);

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
    } catch (err: unknown) {
      // HTTP 410 = signal expired while waiting for manual approval
      const status = (err as { status?: number })?.status
        ?? (err as { response?: { status?: number } })?.response?.status;
      if (status === 410) {
        updateSignalStatus(s.id, "expired");
        pushNotification({
          type: "error",
          title: "Signal expired",
          message: `${s.symbol} ${s.strategy} — market conditions may have changed`,
        });
      } else {
        pushNotification({ type: "error", title: "Approve failed", message: s.id });
      }
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
      {visible.map((s) => {
        const isExpired = s.status === "expired";
        const cardOpacity = isExpired ? "opacity-50" : "";

        return (
          <div
            key={s.id}
            className={`bg-gray-800/60 border border-gray-700 rounded-xl p-4 space-y-3 ${cardOpacity}`}
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
                {s.timeframe && (
                  <span className="text-xs px-1.5 py-0.5 rounded bg-gray-700/70 text-gray-400 font-mono">
                    {s.timeframe}
                  </span>
                )}
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

            {/* Expiry countdown — only for pending signals with an expiry */}
            {s.status === "pending" && s.expires_at && (
              <div className="flex items-center gap-2">
                <ExpiryCountdown expiresAt={s.expires_at} />
                <span className="text-xs text-gray-500">before signal expires</span>
              </div>
            )}

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

            {/* Expiry / rejection reason */}
            {(s.status === "failed" || s.status === "rejected" || s.status === "expired") &&
              (s.rejection_reason || s.reason) && (
              <p className="text-xs text-gray-400 bg-gray-900/60 rounded px-2 py-1.5 border border-gray-700">
                {s.rejection_reason ?? s.reason}
              </p>
            )}

            {/* Actions — only for pending signals that have not expired locally */}
            {s.status === "pending" && (
              <PendingActions
                s={s}
                rejectReason={rejectReason}
                setRejectReason={setRejectReason}
                handleApprove={handleApprove}
                handleReject={handleReject}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
