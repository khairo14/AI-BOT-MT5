"use client";

import { useEffect, useMemo, useState } from "react";
import { fetchAccount, fetchAccounts, switchAccount } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { AccountMode } from "@/types";

interface AccountListItem {
  login: number;
  server: string;
  type?: string;
  account_type?: string;
  account_mode?: string;
  mode?: string;
  broker_name?: string;
}

interface Props {
  onClose: () => void;
}

function readAccountMode(value: unknown): AccountMode | null {
  const mode = String(value || "").toLowerCase().trim();

  if (mode === "live") return "live";
  if (mode === "demo") return "demo";

  return null;
}

function normalizeAccountType(acc?: AccountListItem | null): AccountMode {
  return (
    readAccountMode(acc?.type) ||
    readAccountMode(acc?.account_type) ||
    readAccountMode(acc?.account_mode) ||
    readAccountMode(acc?.mode) ||
    "demo"
  );
}

function withResolvedAccountMode<
  T extends { account_type?: string; account_mode?: string; mode?: string }
>(account: T, fallback: AccountMode) {
  const resolved =
    readAccountMode(account.account_type) ||
    readAccountMode(account.account_mode) ||
    readAccountMode(account.mode) ||
    fallback;

  return {
    ...account,
    account_type: resolved,
    account_mode: resolved,
    mode: resolved,
  };
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export default function TradeSwitchModal({ onClose }: Props) {
  const { setAccount } = useBotStore();

  const [loading, setLoading] = useState(false);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openCount, setOpenCount] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<AccountListItem[]>([]);
  const [selectedLogin, setSelectedLogin] = useState<number | null>(null);

  useEffect(() => {
    const loadAccounts = async () => {
      setLoadingAccounts(true);
      setError(null);

      try {
        const response = await fetchAccounts();

        const sorted = [...(response.accounts || [])].sort((a, b) => {
          const aType = normalizeAccountType(a);
          const bType = normalizeAccountType(b);

          if (aType === bType) return Number(a.login) - Number(b.login);
          return aType === "demo" ? -1 : 1;
        });

        setAccounts(sorted);
        setSelectedLogin(sorted.length > 0 ? Number(sorted[0].login) : null);
      } catch (err) {
        console.error("Failed to load accounts:", err);
        setError("Could not load available accounts.");
      } finally {
        setLoadingAccounts(false);
      }
    };

    loadAccounts();
  }, []);

  const selectedAccount = useMemo(
    () =>
      accounts.find((acc) => Number(acc.login) === Number(selectedLogin)) ??
      null,
    [accounts, selectedLogin]
  );

  const selectedMode = normalizeAccountType(selectedAccount);
  const selectedLabel = selectedMode === "live" ? "LIVE" : "DEMO";

  const attempt = async (force: boolean) => {
    if (!selectedLogin) {
      setError("No account selected.");
      return;
    }

    setLoading(true);
    setError(null);
    setOpenCount(null);

    const fallbackMode = normalizeAccountType(selectedAccount);

    const verifySwitchedAccount = async () => {
      for (let i = 0; i < 12; i += 1) {
        try {
          await new Promise((resolve) => setTimeout(resolve, 800));

          const fresh = await fetchAccount();

          if (Number(fresh?.login) === Number(selectedLogin)) {
            setAccount(withResolvedAccountMode(fresh, fallbackMode));
            setError(null);
            onClose();
            return true;
          }
        } catch (verifyErr) {
          console.warn(`Switch verification retry ${i + 1} failed:`, verifyErr);
        }
      }

      return false;
    };

    try {
      const res = await switchAccount(selectedLogin, force);

      if (res.account) {
        setAccount(withResolvedAccountMode(res.account, fallbackMode));
      } else {
        const fresh = await fetchAccount();
        setAccount(withResolvedAccountMode(fresh, fallbackMode));
      }

      setError(null);
      onClose();
    } catch (err: unknown) {
      console.error("Switch account error:", err);

      const detail = (
        err as {
          response?: {
            data?: {
              detail?:
                | string
                | {
                    message?: string;
                    open_count?: number;
                    error?: string;
                  };
            };
          };
        }
      )?.response?.data?.detail;

      if (
        detail &&
        typeof detail === "object" &&
        detail.error === "open_positions"
      ) {
        setError(detail.message ?? "Open positions exist.");
        setOpenCount(detail.open_count ?? null);
        return;
      }

      const verified = await verifySwitchedAccount();

      if (verified) {
        return;
      }

      if (typeof detail === "string") {
        setError(detail);
      } else {
        setError("Switch failed. Check MT5 terminal.");
      }
    } finally {
      setLoading(false);
    }
  };

  if (loadingAccounts) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
        <div className="w-full max-w-sm rounded-2xl border border-gray-700 bg-gray-900 p-6 shadow-2xl">
          <div className="flex items-center justify-center py-8">
            <div className="h-8 w-8 animate-spin rounded-full border-b-2 border-blue-500" />
            <span className="ml-3 text-gray-400">Loading accounts...</span>
          </div>
        </div>
      </div>
    );
  }

  if (accounts.length === 0) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
        <div className="w-full max-w-sm rounded-2xl border border-gray-700 bg-gray-900 p-6 shadow-2xl">
          <h2 className="mb-2 text-lg font-bold text-white">
            No Accounts Configured
          </h2>

          <p className="mb-4 text-sm text-gray-400">
            No MT5 accounts are configured. Please add a demo or live account in
            settings.
          </p>

          {error && (
            <div className="mb-4 rounded-lg border border-red-700 bg-red-900/40 p-3 text-sm text-red-300">
              {error}
            </div>
          )}

          <button
            onClick={onClose}
            className="w-full rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700"
          >
            Close
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-2xl border border-gray-700 bg-gray-900 p-6 shadow-2xl">
        <h2 className="mb-1 text-lg font-bold text-white">Switch Account</h2>

        <p className="mb-4 text-sm text-gray-400">
          Select the MT5 account the bot should connect to. Demo and live
          accounts are separated.
        </p>

        <div className="mb-4">
          <label className="mb-2 block text-xs uppercase tracking-wide text-gray-500">
            Select Account
          </label>

          <select
            value={selectedLogin ?? ""}
            onChange={(e) => setSelectedLogin(Number(e.target.value))}
            className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-white focus:border-blue-500 focus:outline-none"
          >
            {accounts.map((acc) => {
              const mode = normalizeAccountType(acc);
              const label = mode === "live" ? "LIVE" : "DEMO";

              return (
                <option key={acc.login} value={acc.login}>
                  [{label}] {acc.login} - {acc.server}
                  {acc.broker_name ? ` (${acc.broker_name})` : ""}
                </option>
              );
            })}
          </select>
        </div>

        {selectedAccount && (
          <div
            className={`mb-4 rounded-lg border p-3 text-sm ${
              selectedMode === "live"
                ? "border-red-700 bg-red-900/30 text-red-200"
                : "border-emerald-700 bg-emerald-900/30 text-emerald-200"
            }`}
          >
            Switching to <span className="font-bold">{selectedLabel}</span>{" "}
            account <span className="font-bold">{selectedAccount.login}</span>.
            {selectedMode === "live" && (
              <p className="mt-1 text-xs text-red-300">
                Live account selected. Real funds may be at risk.
              </p>
            )}
          </div>
        )}

        {error && (
          <div className="mb-4 rounded-lg border border-red-700 bg-red-900/40 p-3 text-sm text-red-300">
            <p className="mb-1 font-semibold">
              {openCount !== null ? `${openCount} open position(s)` : "Error"}
            </p>

            <p>{error}</p>

            {openCount !== null && (
              <button
                onClick={() => attempt(true)}
                disabled={loading}
                className="mt-2 text-xs text-red-200 underline hover:text-white disabled:opacity-50"
              >
                Switch anyway — positions stay open on current account
              </button>
            )}
          </div>
        )}

        <div className="mt-2 flex gap-3">
          <button
            onClick={onClose}
            className="flex-1 rounded-lg bg-gray-800 px-4 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700"
          >
            Cancel
          </button>

          <button
            onClick={() => attempt(false)}
            disabled={loading || !selectedLogin}
            className={`flex-1 rounded-lg px-4 py-2 text-sm font-semibold text-white transition-colors disabled:opacity-50 ${
              selectedMode === "live"
                ? "bg-red-600 hover:bg-red-700"
                : "bg-emerald-600 hover:bg-emerald-700"
            }`}
          >
            {loading ? "Switching…" : `Switch to ${selectedLabel}`}
          </button>
        </div>
      </div>
    </div>
  );
}