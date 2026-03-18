"use client";
import { useState } from "react";
import { switchMode, fetchAccount } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { AccountMode } from "@/types";

interface Props {
  targetMode: AccountMode;
  onClose: () => void;
}

export default function TradeSwitchModal({ targetMode, onClose }: Props) {
  const { setAccount } = useBotStore();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openCount, setOpenCount] = useState<number | null>(null);

  const label = targetMode === "live" ? "LIVE" : "PAPER / DEMO";
  const accent = targetMode === "live" ? "red" : "emerald";

  const attempt = async (force: boolean) => {
    setLoading(true);
    setError(null);
    try {
      const res = await switchMode(targetMode, force);
      if (res.account) setAccount(res.account);
      else {
        const fresh = await fetchAccount();
        setAccount(fresh);
      }
      onClose();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: { message?: string; open_count?: number; error?: string } } } })
        ?.response?.data?.detail;
      if (detail && typeof detail === "object" && detail.error === "open_positions") {
        setError(detail.message ?? "Open positions exist.");
        setOpenCount(detail.open_count ?? null);
      } else if (typeof detail === "string") {
        setError(detail);
      } else {
        setError("Switch failed. Check MT5 terminal.");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-gray-900 border border-gray-700 rounded-2xl p-6 w-full max-w-sm shadow-2xl">
        {/* Header */}
        <h2 className="text-lg font-bold text-white mb-1">Switch to {label}</h2>
        <p className="text-sm text-gray-400 mb-4">
          {targetMode === "live"
            ? "Real money will be at risk. All bot signals will execute on your live MT5 account."
            : "Switching to demo account. No real funds at risk."}
        </p>

        {/* Error / warning block */}
        {error && (
          <div className="bg-red-900/40 border border-red-700 rounded-lg p-3 mb-4 text-sm text-red-300">
            <p className="font-semibold mb-1">
              {openCount !== null ? `${openCount} open position(s)` : "Error"}
            </p>
            <p>{error}</p>
            {openCount !== null && (
              <button
                onClick={() => attempt(true)}
                disabled={loading}
                className="mt-2 text-xs text-red-200 underline hover:text-white"
              >
                Switch anyway (positions stay open on current account)
              </button>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-3 mt-2">
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 rounded-lg text-sm bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => attempt(false)}
            disabled={loading}
            className={`flex-1 px-4 py-2 rounded-lg text-sm font-semibold text-white transition-colors disabled:opacity-50 ${
              targetMode === "live"
                ? "bg-red-600 hover:bg-red-700"
                : "bg-emerald-600 hover:bg-emerald-700"
            }`}
          >
            {loading ? "Switching…" : `Confirm ${label}`}
          </button>
        </div>
      </div>
    </div>
  );
}
