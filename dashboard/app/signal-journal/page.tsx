"use client";

import { useEffect, useMemo, useState } from "react";

type SignalDecision =
  | "taken"
  | "skipped"
  | "blocked_by_rl"
  | "blocked_by_risk"
  | "blocked_by_filter"
  | "shadow_only"
  | string;

type SignalRow = {
  signal_id?: string;
  recorded_at?: string;
  timestamp?: string;
  symbol?: string;
  symbol_raw?: string;
  symbol_normalized?: string;
  strategy?: string;
  trading_type?: string;
  mode?: string;
  execution_mode?: string;
  account_type?: string;
  account_login?: number | string;
  direction?: string;
  confidence?: number;
  score?: number;
  entry?: number | string | null;
  entry_price?: number | string | null;
  sl?: number | string | null;
  sl_price?: number | string | null;
  tp?: number | string | null;
  tp_price?: number | string | null;
  status?: string;
  decision?: SignalDecision;
  reason?: string;
  timeframe?: string;
  filters?: Record<string, unknown>;
  validated?: boolean;
  validated_outcome?: string | null;
  future_profit_pips?: number | null;
  future_profit_pct?: number | null;
  validated_at?: string | null;
};

type StatsResponse = {
  success: boolean;
  total_signals?: number;
  pending_validation?: number;
  validated_wins?: number;
  validated_losses?: number;
  decisions?: Record<string, number>;
  reasons?: Record<string, number>;
  strategies?: Record<string, number>;
  error?: string;
};

type JournalResponse = {
  success: boolean;
  count?: number;
  rows?: SignalRow[];
  error?: string;
};

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ||
  "http://127.0.0.1:8000";

const DECISION_OPTIONS = [
  "all",
  "blocked_by_risk",
  "blocked_by_filter",
  "blocked_by_rl",
  "shadow_only",
  "taken",
  "skipped",
];

function formatTime(value?: string): string {
  if (!value) return "—";

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;

  return date.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

function formatPrice(value: unknown): string {
  const num = toNumber(value);
  if (num === null) return "—";
  if (Math.abs(num) >= 100) return num.toFixed(2);
  if (Math.abs(num) >= 1) return num.toFixed(5);
  return num.toFixed(6);
}

function formatPct(value: unknown): string {
  const num = toNumber(value);
  if (num === null) return "—";
  return `${(num * 100).toFixed(1)}%`;
}

function getRowTime(row: SignalRow): string {
  return row.recorded_at || row.timestamp || "";
}

function getSymbol(row: SignalRow): string {
  return row.symbol_raw || row.symbol || row.symbol_normalized || "—";
}

function getMode(row: SignalRow): string {
  return row.trading_type || row.mode || "—";
}

function getEntry(row: SignalRow): unknown {
  return row.entry ?? row.entry_price;
}

function getSl(row: SignalRow): unknown {
  return row.sl ?? row.sl_price;
}

function getTp(row: SignalRow): unknown {
  return row.tp ?? row.tp_price;
}

function badgeClass(kind: string): string {
  const normalized = kind.toLowerCase();

  if (normalized.includes("risk") || normalized.includes("loss")) {
    return "bg-red-950/60 text-red-300 border-red-700/50";
  }

  if (normalized.includes("filter") || normalized.includes("warning")) {
    return "bg-yellow-950/60 text-yellow-300 border-yellow-700/50";
  }

  if (normalized.includes("win") || normalized === "taken" || normalized === "executed") {
    return "bg-emerald-950/60 text-emerald-300 border-emerald-700/50";
  }

  if (normalized.includes("shadow")) {
    return "bg-purple-950/60 text-purple-300 border-purple-700/50";
  }

  return "bg-gray-900 text-gray-300 border-gray-700";
}

function StatCard({
  label,
  value,
  detail,
}: {
  label: string;
  value: string | number;
  detail?: string;
}) {
  return (
    <div className="rounded-xl border border-gray-800 bg-gray-950 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-gray-500">
        {label}
      </p>
      <p className="mt-2 text-2xl font-bold text-white">{value}</p>
      {detail && <p className="mt-1 text-xs text-gray-500">{detail}</p>}
    </div>
  );
}

export default function SignalJournalPage() {
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [stats, setStats] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [decision, setDecision] = useState("all");
  const [validatedFilter, setValidatedFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(250);

  const load = async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      params.set("limit", String(limit));

      if (decision !== "all") params.set("decision", decision);
      if (validatedFilter === "validated") params.set("validated", "true");
      if (validatedFilter === "pending") params.set("validated", "false");

      const [journalRes, statsRes] = await Promise.all([
        fetch(`${API_BASE}/signal-journal/?${params.toString()}`),
        fetch(`${API_BASE}/signal-journal/stats`),
      ]);

      const journalData = (await journalRes.json()) as JournalResponse;
      const statsData = (await statsRes.json()) as StatsResponse;

      if (!journalData.success) {
        throw new Error(journalData.error || "Signal journal request failed");
      }

      if (!statsData.success) {
        throw new Error(statsData.error || "Signal journal stats request failed");
      }

      setRows(journalData.rows || []);
      setStats(statsData);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to load signal journal");
      setRows([]);
      setStats(null);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [decision, validatedFilter, limit]);

  const filteredRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rows;

    return rows.filter((row) => {
      const haystack = [
        getSymbol(row),
        row.strategy,
        getMode(row),
        row.direction,
        row.status,
        row.decision,
        row.reason,
        row.validated_outcome,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();

      return haystack.includes(q);
    });
  }, [rows, search]);

  const winRate = useMemo(() => {
    const wins = stats?.validated_wins || 0;
    const losses = stats?.validated_losses || 0;
    const total = wins + losses;
    return total > 0 ? `${((wins / total) * 100).toFixed(1)}%` : "—";
  }, [stats]);

  return (
    <main className="min-h-screen bg-gray-950 text-gray-100 p-6 space-y-6">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <p className="text-sm font-semibold uppercase tracking-[0.2em] text-blue-400">
            Tools
          </p>
          <h1 className="mt-2 text-3xl font-bold text-white">Signal Journal</h1>
          <p className="mt-2 max-w-3xl text-sm text-gray-400">
            Review generated, blocked, rejected, expired, and shadow/counterfactual
            signals. Auto-validation and shadow learning controls are visible here,
            but learning from non-executed signals remains disabled.
          </p>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => void load()}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-500 disabled:opacity-60"
            disabled={loading}
          >
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      </div>

      <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-5">
        <StatCard label="Total Signals" value={stats?.total_signals ?? 0} />
        <StatCard label="Pending Validation" value={stats?.pending_validation ?? 0} />
        <StatCard label="Validated Wins" value={stats?.validated_wins ?? 0} />
        <StatCard label="Validated Losses" value={stats?.validated_losses ?? 0} />
        <StatCard label="Validation Win Rate" value={winRate} />
      </section>

      <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-gray-800 bg-gray-950 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-300">
                Auto Validation
              </h2>
              <p className="mt-1 text-xs text-gray-500">
                Approved setup: enabled later after validation engine is added.
              </p>
            </div>
            <span className="rounded-full border border-yellow-700/50 bg-yellow-950/60 px-3 py-1 text-xs font-bold text-yellow-300">
              Pending Engine
            </span>
          </div>
          <div className="mt-4 grid grid-cols-1 gap-2 text-xs text-gray-400 md:grid-cols-3">
            <div className="rounded-lg bg-gray-900 p-3">
              <p className="font-semibold text-gray-300">Scalping</p>
              <p className="mt-1">Validate after 3–6 hours</p>
            </div>
            <div className="rounded-lg bg-gray-900 p-3">
              <p className="font-semibold text-gray-300">Day Trading</p>
              <p className="mt-1">Validate after 2–5 days</p>
            </div>
            <div className="rounded-lg bg-gray-900 p-3">
              <p className="font-semibold text-gray-300">Swing</p>
              <p className="mt-1">Validate after 7–21 days</p>
            </div>
          </div>
        </div>

        <div className="rounded-xl border border-gray-800 bg-gray-950 p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-300">
                Shadow Learning
              </h2>
              <p className="mt-1 text-xs text-gray-500">
                Non-executed signal outcomes are analytics-only for now.
              </p>
            </div>
            <span className="rounded-full border border-red-700/50 bg-red-950/60 px-3 py-1 text-xs font-bold text-red-300">
              Disabled
            </span>
          </div>
          <p className="mt-4 text-sm text-gray-400">
            Executed demo/live trades can remain learning-valid after broker close.
            Blocked, expired, rejected, and counterfactual signals should be validated
            and analyzed first before any future learning toggle is enabled.
          </p>
        </div>
      </section>

      <section className="rounded-xl border border-gray-800 bg-gray-950">
        <div className="flex flex-col gap-3 border-b border-gray-800 p-4 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <h2 className="text-lg font-semibold text-white">Journal Entries</h2>
            <p className="text-sm text-gray-500">
              Showing {filteredRows.length} of {rows.length} loaded rows
            </p>
          </div>

          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search symbol, strategy, reason..."
              className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white outline-none focus:border-blue-500"
            />

            <select
              value={decision}
              onChange={(event) => setDecision(event.target.value)}
              className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white outline-none focus:border-blue-500"
            >
              {DECISION_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {option === "all" ? "All decisions" : option}
                </option>
              ))}
            </select>

            <select
              value={validatedFilter}
              onChange={(event) => setValidatedFilter(event.target.value)}
              className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white outline-none focus:border-blue-500"
            >
              <option value="all">All validation</option>
              <option value="pending">Pending</option>
              <option value="validated">Validated</option>
            </select>

            <select
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
              className="rounded-lg border border-gray-700 bg-gray-900 px-3 py-2 text-sm text-white outline-none focus:border-blue-500"
            >
              <option value={100}>100</option>
              <option value={250}>250</option>
              <option value={500}>500</option>
              <option value={1000}>1000</option>
            </select>
          </div>
        </div>

        {error && (
          <div className="border-b border-red-900/60 bg-red-950/40 p-4 text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-800 text-sm">
            <thead className="bg-gray-900/60 text-xs uppercase tracking-wide text-gray-500">
              <tr>
                <th className="px-4 py-3 text-left">Time</th>
                <th className="px-4 py-3 text-left">Symbol</th>
                <th className="px-4 py-3 text-left">Mode</th>
                <th className="px-4 py-3 text-left">Strategy</th>
                <th className="px-4 py-3 text-left">Dir</th>
                <th className="px-4 py-3 text-left">Confidence</th>
                <th className="px-4 py-3 text-left">Decision</th>
                <th className="px-4 py-3 text-left">Reason</th>
                <th className="px-4 py-3 text-right">Entry</th>
                <th className="px-4 py-3 text-right">SL</th>
                <th className="px-4 py-3 text-right">TP</th>
                <th className="px-4 py-3 text-left">Validation</th>
                <th className="px-4 py-3 text-right">Pips</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-900">
              {loading ? (
                <tr>
                  <td colSpan={13} className="px-4 py-10 text-center text-gray-500">
                    Loading signal journal...
                  </td>
                </tr>
              ) : filteredRows.length === 0 ? (
                <tr>
                  <td colSpan={13} className="px-4 py-10 text-center text-gray-500">
                    No signal journal entries found yet.
                  </td>
                </tr>
              ) : (
                filteredRows.map((row, index) => {
                  const decisionValue = row.decision || row.status || "unknown";
                  const validationLabel = row.validated
                    ? row.validated_outcome || "validated"
                    : "pending";

                  return (
                    <tr key={`${row.signal_id || getRowTime(row)}-${index}`} className="hover:bg-gray-900/60">
                      <td className="whitespace-nowrap px-4 py-3 text-gray-400">
                        {formatTime(getRowTime(row))}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 font-semibold text-white">
                        {getSymbol(row)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-gray-300">
                        {getMode(row)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-gray-300">
                        {row.strategy || "—"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className={row.direction === "BUY" ? "text-emerald-300" : "text-red-300"}>
                          {row.direction || "—"}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-gray-300">
                        {formatPct(row.confidence ?? row.score)}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className={`rounded-full border px-2 py-1 text-xs font-semibold ${badgeClass(decisionValue)}`}>
                          {decisionValue}
                        </span>
                      </td>
                      <td className="max-w-[260px] truncate px-4 py-3 text-gray-400" title={row.reason || ""}>
                        {row.reason || "—"}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-gray-300">
                        {formatPrice(getEntry(row))}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-gray-300">
                        {formatPrice(getSl(row))}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-gray-300">
                        {formatPrice(getTp(row))}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3">
                        <span className={`rounded-full border px-2 py-1 text-xs font-semibold ${badgeClass(validationLabel)}`}>
                          {validationLabel}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-right font-mono text-gray-300">
                        {row.future_profit_pips ?? "—"}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  );
}
