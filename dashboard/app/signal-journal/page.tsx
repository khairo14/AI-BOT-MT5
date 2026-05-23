"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

type SignalRow = {
  signal_id?: string;
  recorded_at?: string;
  timestamp?: string;
  signal_time?: string;
  created_at?: string;
  symbol_raw?: string;
  symbol_normalized?: string;
  symbol?: string;
  trading_type?: string;
  mode?: string;
  timeframe?: string;
  tf?: string;
  strategy?: string;
  direction?: string;
  confidence?: number;
  score?: number;
  decision?: string;
  reason?: string;
  status?: string;
  entry?: number;
  entry_price?: number;
  sl?: number;
  sl_price?: number;
  tp?: number;
  tp_price?: number;
  validated?: boolean;
  validation_status?: string;
  validated_outcome?: string;
  future_profit_pips?: number;
  max_favorable_pips?: number;
  max_adverse_pips?: number;
  account_type?: string;
  account_login?: number | string;
  filters?: Record<string, unknown>;
};

type JournalResponse = {
  success: boolean;
  count?: number;
  rows?: SignalRow[];
  error?: string;
};

type JournalStats = {
  success: boolean;
  total_signals: number;
  pending_validation: number;
  validated_wins: number;
  validated_losses: number;
  decisions: Record<string, number>;
  reasons: Record<string, number>;
  strategies: Record<string, number>;
};

type ValidationStats = {
  success: boolean;
  total_validated: number;
  wins: number;
  losses: number;
  no_hit: number;
  winrate: number;
  outcomes: Record<string, number>;
};

type RunValidationResponse = {
  success: boolean;
  checked: number;
  validated: number;
  skipped: number;
  not_ready: number;
  pending_seen: number;
  errors?: Array<Record<string, unknown>>;
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";

const EXPIRY_SECONDS_BY_TF: Record<string, number> = {
  M1: 60,
  M5: 300,
  M15: 900,
  M30: 1800,
  H1: 3600,
  H4: 14400,
  D1: 86400,
  W1: 604800,
};

function classNames(...items: Array<string | false | null | undefined>) {
  return items.filter(Boolean).join(" ");
}

function fmtDate(value?: string) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function fmtNum(value: unknown, digits = 2) {
  const num = Number(value);
  if (!Number.isFinite(num)) return "—";
  return num.toFixed(digits);
}

function signalTime(row: SignalRow) {
  const status = String(row.status || "").toLowerCase();

  // For workflow/terminal rows, preserve original signal time.
  if (
    status.includes("expired") ||
    status.includes("rejected") ||
    status.includes("blocked") ||
    status.includes("executed") ||
    status.includes("failed")
  ) {
    return row.created_at || row.signal_time || row.timestamp || row.recorded_at || "";
  }

  return row.created_at || row.signal_time || row.timestamp || row.recorded_at || "";
}

function rowSymbol(row: SignalRow) {
  return row.symbol_raw || row.symbol_normalized || row.symbol || "—";
}

function rowMode(row: SignalRow) {
  return row.trading_type || row.mode || "—";
}

function rowTimeframe(row: SignalRow) {
  const explicit = row.timeframe || row.tf;
  if (explicit) return explicit.toUpperCase();

  const mode = rowMode(row).toLowerCase();
  if (mode === "scalping") return "M5";
  if (mode === "swing") return "H4";
  return "H1";
}

function rowEntry(row: SignalRow) {
  return row.entry ?? row.entry_price;
}

function rowSl(row: SignalRow) {
  return row.sl ?? row.sl_price;
}

function rowTp(row: SignalRow) {
  return row.tp ?? row.tp_price;
}

function outcomeLabel(outcome?: string) {
  if (!outcome) return "Pending";
  if (outcome === "would_tp") return "Win";
  if (outcome === "would_sl") return "Loss";
  if (outcome === "no_hit") return "No hit";
  return outcome.replaceAll("_", " ");
}

function outcomeClass(outcome?: string) {
  if (outcome === "would_tp") return "text-emerald-300 bg-emerald-950/40 border-emerald-600/40";
  if (outcome === "would_sl") return "text-red-300 bg-red-950/40 border-red-600/40";
  if (outcome === "no_hit") return "text-yellow-300 bg-yellow-950/40 border-yellow-600/40";
  return "text-slate-300 bg-slate-900 border-slate-700";
}

function decisionClass(decision?: string) {
  const value = String(decision || "").toLowerCase();
  if (value.includes("risk")) return "text-red-300 bg-red-950/40 border-red-600/40";
  if (value.includes("filter")) return "text-yellow-300 bg-yellow-950/40 border-yellow-600/40";
  if (value.includes("executed") || value.includes("approved")) return "text-emerald-300 bg-emerald-950/40 border-emerald-600/40";
  if (value.includes("expired") || value.includes("ignored")) return "text-orange-300 bg-orange-950/40 border-orange-600/40";
  return "text-blue-300 bg-blue-950/40 border-blue-600/40";
}

function getExpiryInfo(row: SignalRow) {
  const timeText = signalTime(row);
  const started = timeText ? new Date(timeText) : null;

  if (!started || Number.isNaN(started.getTime())) {
    return {
      label: "—",
      expired: false,
      secondsRemaining: null as number | null,
      expiresAt: null as Date | null,
    };
  }

  const tf = rowTimeframe(row);
  const expirySeconds = EXPIRY_SECONDS_BY_TF[tf] ?? 3600;
  const expiresAt = new Date(started.getTime() + expirySeconds * 1000);
  const secondsRemaining = Math.floor((expiresAt.getTime() - Date.now()) / 1000);
  const expired = secondsRemaining <= 0;
  const terminal =
    String(row.status || "").toLowerCase().includes("expired") ||
    String(row.status || "").toLowerCase().includes("rejected") ||
    String(row.status || "").toLowerCase().includes("blocked") ||
    String(row.status || "").toLowerCase().includes("executed") ||
    String(row.status || "").toLowerCase().includes("failed");

  if (terminal) {
    return {
      label: "—",
      expired: true,
      secondsRemaining: 0,
      expiresAt,
    };
  }

  if (expired) {
    return {
      label: `Expired ${fmtDate(expiresAt.toISOString())}`,
      expired,
      secondsRemaining,
      expiresAt,
    };
  }

  const mins = Math.floor(secondsRemaining / 60);
  const secs = secondsRemaining % 60;
  const label = mins > 0 ? `${mins}m ${secs}s left` : `${secs}s left`;

  return {
    label,
    expired,
    secondsRemaining,
    expiresAt,
  };
}

function classifyWorkflow(row: SignalRow) {
  const status = String(row.status || "").toLowerCase();
  const decision = String(row.decision || "").toLowerCase();

  if (status.includes("executed") || decision.includes("executed")) return "executed";
  if (status.includes("approved") || decision.includes("approved")) return "approved";
  if (status.includes("expired") || decision.includes("expired")) return "expired";
  if (status.includes("rejected") || decision.includes("rejected")) return "rejected";
  if (status.includes("blocked") || decision.includes("blocked")) return "blocked";
  if (status.includes("shadow") || decision.includes("shadow")) return "shadow";
  return "pending";
}

function StatCard({
  label,
  value,
  sub,
  tone = "default",
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: "default" | "good" | "warn" | "bad";
}) {
  const toneClass =
    tone === "good"
      ? "border-emerald-700/50 bg-emerald-950/10"
      : tone === "warn"
        ? "border-yellow-700/50 bg-yellow-950/10"
        : tone === "bad"
          ? "border-red-700/50 bg-red-950/10"
          : "border-slate-800 bg-slate-950/40";

  return (
    <div className={classNames("rounded-xl border p-5", toneClass)}>
      <div className="text-xs font-bold uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-3 font-mono text-3xl font-black text-white">{value}</div>
      {sub ? <div className="mt-2 text-sm text-slate-400">{sub}</div> : null}
    </div>
  );
}

function Breakdown({
  title,
  data,
  limit = 8,
}: {
  title: string;
  data: Record<string, number>;
  limit?: number;
}) {
  const items = Object.entries(data || {})
    .sort((a, b) => b[1] - a[1])
    .slice(0, limit);

  const max = Math.max(...items.map(([, value]) => value), 1);

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
      <div className="mb-4 text-sm font-bold uppercase tracking-wide text-slate-300">{title}</div>
      {items.length === 0 ? (
        <div className="text-sm text-slate-500">No data yet.</div>
      ) : (
        <div className="space-y-3">
          {items.map(([key, value]) => (
            <div key={key}>
              <div className="mb-1 flex items-center justify-between gap-3 text-sm">
                <span className="truncate text-slate-300">{key || "unknown"}</span>
                <span className="font-mono text-slate-400">{value}</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-slate-800">
                <div
                  className="h-full rounded-full bg-blue-500"
                  style={{ width: `${Math.max(4, (value / max) * 100)}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function WorkflowBreakdown({ rows }: { rows: SignalRow[] }) {
  const data = rows.reduce<Record<string, number>>((acc, row) => {
    const key = classifyWorkflow(row);
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});

  return <Breakdown title="Ignored / Approved / Executed" data={data} />;
}

export default function SignalJournalPage() {
  const [rows, setRows] = useState<SignalRow[]>([]);
  const [stats, setStats] = useState<JournalStats | null>(null);
  const [validationStats, setValidationStats] = useState<ValidationStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [runningValidation, setRunningValidation] = useState(false);
  const [lastValidation, setLastValidation] = useState<RunValidationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [, setNowTick] = useState(0);

  const [search, setSearch] = useState("");
  const [decision, setDecision] = useState("all");
  const [validationFilter, setValidationFilter] = useState("all");
  const [limit, setLimit] = useState(250);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams();
      params.set("limit", String(limit));

      if (decision !== "all") params.set("decision", decision);
      if (validationFilter === "validated") params.set("validated", "true");
      if (validationFilter === "pending") params.set("validated", "false");

      const [journalRes, statsRes, validationRes] = await Promise.all([
        fetch(`${API_BASE}/signal-journal/?${params.toString()}`),
        fetch(`${API_BASE}/signal-journal/stats`),
        fetch(`${API_BASE}/signal-journal/validation-stats`),
      ]);

      if (!journalRes.ok) throw new Error(`Journal request failed: ${journalRes.status}`);
      if (!statsRes.ok) throw new Error(`Stats request failed: ${statsRes.status}`);
      if (!validationRes.ok) throw new Error(`Validation stats request failed: ${validationRes.status}`);

      const journalJson = (await journalRes.json()) as JournalResponse;
      const statsJson = (await statsRes.json()) as JournalStats;
      const validationJson = (await validationRes.json()) as ValidationStats;

      setRows(journalJson.rows || []);
      setStats(statsJson);
      setValidationStats(validationJson);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to load signal journal");
    } finally {
      setLoading(false);
    }
  }, [decision, limit, validationFilter]);

  useEffect(() => {
    void loadData();

    const interval = window.setInterval(() => {
      void loadData();
    }, 60_000);

    return () => window.clearInterval(interval);
  }, [loadData]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      setNowTick((value) => value + 1);
    }, 1000);

    return () => window.clearInterval(interval);
  }, []);

  const runValidation = async () => {
    setRunningValidation(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/signal-journal/run-validation?max_signals=100`, {
        method: "POST",
      });

      if (!res.ok) throw new Error(`Run validation failed: ${res.status}`);

      const json = (await res.json()) as RunValidationResponse;
      setLastValidation(json);
      await loadData();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Failed to run validation");
    } finally {
      setRunningValidation(false);
    }
  };

  const filteredRows = useMemo(() => {
    const term = search.trim().toLowerCase();

    if (!term) return rows;

    return rows.filter((row) => {
      const haystack = [
        rowSymbol(row),
        rowMode(row),
        rowTimeframe(row),
        row.strategy,
        row.direction,
        row.decision,
        row.reason,
        row.status,
        row.validated_outcome,
        row.account_type,
        row.account_login,
      ]
        .join(" ")
        .toLowerCase();

      return haystack.includes(term);
    });
  }, [rows, search]);

  const workflowCounts = useMemo(() => {
    return rows.reduce<Record<string, number>>((acc, row) => {
      const key = classifyWorkflow(row);
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
  }, [rows]);

  const activeManualSignals = useMemo(() => {
    return rows.filter((row) => {
      const workflow = classifyWorkflow(row);
      const expiry = getExpiryInfo(row);
      return workflow === "pending" && !expiry.expired;
    }).length;
  }, [rows]);

  const validatedTotal = validationStats?.total_validated || 0;
  const winrate = validationStats?.winrate ?? 0;
  const totalSignals = stats?.total_signals || 0;
  const pendingValidation = stats?.pending_validation || 0;
  const executedOrApproved = (workflowCounts.executed || 0) + (workflowCounts.approved || 0);
  const ignoredOrExpired = (workflowCounts.expired || 0) + (workflowCounts.rejected || 0);

  return (
    <main className="min-h-screen bg-slate-950 px-6 py-8 text-white">
      <div className="mx-auto max-w-7xl space-y-7">
        <section className="flex flex-col justify-between gap-4 md:flex-row md:items-start">
          <div>
            <div className="text-sm font-bold uppercase tracking-[0.35em] text-blue-400">Tools</div>
            <h1 className="mt-3 text-4xl font-black tracking-tight">Signal Journal</h1>
            <p className="mt-3 max-w-4xl text-sm leading-6 text-slate-300">
              Review generated, blocked, rejected, expired, and counterfactual signals. Auto-validation is enabled for analytics;
              shadow learning remains disabled.
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <button
              onClick={() => void runValidation()}
              disabled={runningValidation}
              className="rounded-lg bg-emerald-600 px-5 py-3 text-sm font-bold text-white shadow-lg shadow-emerald-950/40 transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {runningValidation ? "Running..." : "Run Validation"}
            </button>
            <button
              onClick={() => void loadData()}
              disabled={loading}
              className="rounded-lg bg-blue-600 px-5 py-3 text-sm font-bold text-white shadow-lg shadow-blue-950/40 transition hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        </section>

        {error ? (
          <div className="rounded-xl border border-red-700/50 bg-red-950/30 p-4 text-sm text-red-200">
            {error}
          </div>
        ) : null}

        {lastValidation ? (
          <div className="rounded-xl border border-emerald-700/40 bg-emerald-950/20 p-4 text-sm text-emerald-100">
            Validation run complete: checked {lastValidation.checked}, validated {lastValidation.validated}, skipped{" "}
            {lastValidation.skipped}, not ready {lastValidation.not_ready}, pending seen {lastValidation.pending_seen}.
          </div>
        ) : null}

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-6">
          <StatCard label="Total Signals" value={totalSignals} />
          <StatCard label="Active Manual" value={activeManualSignals} tone={activeManualSignals > 0 ? "warn" : "default"} />
          <StatCard label="Pending Validation" value={pendingValidation} tone={pendingValidation > 0 ? "warn" : "default"} />
          <StatCard label="Validated" value={validatedTotal} />
          <StatCard label="Validated Wins" value={validationStats?.wins || 0} tone="good" />
          <StatCard label="Validation Win Rate" value={validatedTotal > 0 ? `${winrate.toFixed(1)}%` : "—"} />
        </section>

        <section className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-4">
          <StatCard label="Blocked By Risk" value={stats?.decisions?.blocked_by_risk || 0} tone={(stats?.decisions?.blocked_by_risk || 0) > 0 ? "bad" : "default"} />
          <StatCard label="Blocked By Filter" value={stats?.decisions?.blocked_by_filter || 0} tone={(stats?.decisions?.blocked_by_filter || 0) > 0 ? "warn" : "default"} />
          <StatCard label="Approved / Executed" value={executedOrApproved} tone="good" />
          <StatCard label="Ignored / Expired / Rejected" value={ignoredOrExpired} tone={ignoredOrExpired > 0 ? "warn" : "default"} />
        </section>

        <section className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-lg font-black uppercase tracking-wide">Auto Validation</div>
                <p className="mt-2 text-sm text-slate-400">
                  Enabled. Runs automatically every 15 minutes from the backend and can be triggered manually here.
                </p>
              </div>
              <span className="rounded-full border border-emerald-600/40 bg-emerald-950/40 px-4 py-2 text-sm font-black text-emerald-300">
                Enabled
              </span>
            </div>

            <div className="mt-5 grid grid-cols-1 gap-3 sm:grid-cols-3">
              <div className="rounded-lg bg-slate-900 p-4">
                <div className="font-bold">Scalping</div>
                <div className="mt-2 text-sm text-slate-300">Validate after 6 hours</div>
                <div className="mt-1 text-xs text-slate-500">Manual signal expiry usually M5 = 5 min</div>
              </div>
              <div className="rounded-lg bg-slate-900 p-4">
                <div className="font-bold">Day Trading</div>
                <div className="mt-2 text-sm text-slate-300">Validate after 5 days</div>
                <div className="mt-1 text-xs text-slate-500">Manual expiry usually H1 = 1 hour</div>
              </div>
              <div className="rounded-lg bg-slate-900 p-4">
                <div className="font-bold">Swing</div>
                <div className="mt-2 text-sm text-slate-300">Validate after 21 days</div>
                <div className="mt-1 text-xs text-slate-500">Manual expiry usually H4 = 4 hours</div>
              </div>
            </div>
          </div>

          <div className="rounded-xl border border-slate-800 bg-slate-950/40 p-5">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-lg font-black uppercase tracking-wide">Shadow Learning</div>
                <p className="mt-2 text-sm text-slate-400">
                  Non-executed signal outcomes are analytics-only. Executed demo/live trades remain learning-valid after broker close.
                </p>
              </div>
              <span className="rounded-full border border-red-600/40 bg-red-950/40 px-4 py-2 text-sm font-black text-red-300">
                Disabled
              </span>
            </div>
            <p className="mt-5 text-sm leading-6 text-slate-300">
              Counterfactual validation does not rewrite RL qtables, train LSTM, or mark signals as learning-valid.
            </p>
          </div>
        </section>

        <section className="grid grid-cols-1 gap-4 xl:grid-cols-4">
          <Breakdown title="Blocked by Reason" data={stats?.reasons || {}} />
          <Breakdown title="Strategy Counts" data={stats?.strategies || {}} />
          <Breakdown title="Validation Outcomes" data={validationStats?.outcomes || {}} />
          <WorkflowBreakdown rows={rows} />
        </section>

        <section className="rounded-xl border border-slate-800 bg-slate-950/40">
          <div className="flex flex-col gap-4 border-b border-slate-800 p-5 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h2 className="text-2xl font-black">Journal Entries</h2>
              <p className="mt-1 text-sm text-slate-400">
                Showing {filteredRows.length} of {rows.length} loaded rows. Auto-refresh every 60 seconds.
              </p>
            </div>

            <div className="flex flex-col gap-3 sm:flex-row">
              <input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search symbol, strategy, reason..."
                className="min-w-[260px] rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-white outline-none placeholder:text-slate-500 focus:border-blue-500"
              />

              <select
                value={decision}
                onChange={(event) => setDecision(event.target.value)}
                className="rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-white outline-none focus:border-blue-500"
              >
                <option value="all">All decisions</option>
                {Object.keys(stats?.decisions || {}).map((item) => (
                  <option key={item} value={item}>
                    {item}
                  </option>
                ))}
              </select>

              <select
                value={validationFilter}
                onChange={(event) => setValidationFilter(event.target.value)}
                className="rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-white outline-none focus:border-blue-500"
              >
                <option value="all">All validation</option>
                <option value="validated">Validated</option>
                <option value="pending">Pending</option>
              </select>

              <select
                value={limit}
                onChange={(event) => setLimit(Number(event.target.value))}
                className="rounded-lg border border-slate-700 bg-slate-900 px-4 py-3 text-sm text-white outline-none focus:border-blue-500"
              >
                <option value={100}>100</option>
                <option value={250}>250</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
              </select>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="min-w-[1550px] w-full border-collapse text-left text-sm">
              <thead className="bg-slate-900 text-xs uppercase tracking-wide text-slate-400">
                <tr>
                  <th className="px-4 py-4">Time</th>
                  <th className="px-4 py-4">Valid Until</th>
                  <th className="px-4 py-4">Symbol</th>
                  <th className="px-4 py-4">Mode</th>
                  <th className="px-4 py-4">TF</th>
                  <th className="px-4 py-4">Strategy</th>
                  <th className="px-4 py-4">Dir</th>
                  <th className="px-4 py-4">Confidence</th>
                  <th className="px-4 py-4">Decision</th>
                  <th className="px-4 py-4">Reason</th>
                  <th className="px-4 py-4">Entry</th>
                  <th className="px-4 py-4">SL</th>
                  <th className="px-4 py-4">TP</th>
                  <th className="px-4 py-4">Validation</th>
                  <th className="px-4 py-4">Future Pips</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.length === 0 ? (
                  <tr>
                    <td colSpan={15} className="px-4 py-12 text-center text-slate-500">
                      No signal journal entries found yet.
                    </td>
                  </tr>
                ) : (
                  filteredRows.map((row, index) => {
                    const key = row.signal_id || `${signalTime(row)}-${rowSymbol(row)}-${index}`;
                    const expiry = getExpiryInfo(row);
                    return (
                      <tr key={key} className="border-t border-slate-900 hover:bg-slate-900/50">
                        <td className="whitespace-nowrap px-4 py-4 text-slate-300">{fmtDate(signalTime(row))}</td>
                        <td className="whitespace-nowrap px-4 py-4">
                          <span
                            className={classNames(
                              "rounded-full border px-3 py-1 text-xs font-bold",
                              expiry.expired
                                ? "border-orange-600/40 bg-orange-950/40 text-orange-300"
                                : "border-emerald-600/40 bg-emerald-950/40 text-emerald-300",
                            )}
                          >
                            {expiry.label}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-4 font-bold text-white">{rowSymbol(row)}</td>
                        <td className="whitespace-nowrap px-4 py-4 text-slate-300">{rowMode(row)}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">{rowTimeframe(row)}</td>
                        <td className="whitespace-nowrap px-4 py-4 text-slate-300">{row.strategy || "—"}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-bold text-slate-200">{row.direction || "—"}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">{fmtNum(row.confidence, 2)}</td>
                        <td className="whitespace-nowrap px-4 py-4">
                          <span className={classNames("rounded-full border px-3 py-1 text-xs font-bold", decisionClass(row.decision))}>
                            {row.decision || "unknown"}
                          </span>
                        </td>
                        <td className="max-w-[240px] truncate px-4 py-4 text-slate-300">{row.reason || "—"}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">{fmtNum(rowEntry(row), 5)}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">{fmtNum(rowSl(row), 5)}</td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">{fmtNum(rowTp(row), 5)}</td>
                        <td className="whitespace-nowrap px-4 py-4">
                          <span className={classNames("rounded-full border px-3 py-1 text-xs font-bold", outcomeClass(row.validated_outcome))}>
                            {row.validated ? outcomeLabel(row.validated_outcome) : "Pending"}
                          </span>
                        </td>
                        <td className="whitespace-nowrap px-4 py-4 font-mono text-slate-300">
                          {row.future_profit_pips === undefined || row.future_profit_pips === null
                            ? "—"
                            : fmtNum(row.future_profit_pips, 1)}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
      </div>
    </main>
  );
}
