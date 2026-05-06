"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchAnalyticsPerformance, fetchRegimeStatus, fetchExecutionQuality, fetchAccounts } from "@/lib/api";
import type { AnalyticsPerformance, BucketStats, ExecutionQualityMetrics } from "@/types";
import { useBotStore } from "@/lib/store";

// ── helpers ────────────────────────────────────────────────────────────────
function fmt(n: number | undefined, decimals = 2): string {
  if (n == null || isNaN(n)) return "—";
  return n.toFixed(decimals);
}
function pct(n: number | undefined): string {
  if (n == null || isNaN(n)) return "—";
  return `${n.toFixed(1)}%`;
}
function profit(n: number | undefined): string {
  if (n == null || isNaN(n)) return "—";
  const sign = n >= 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}
function clsProfit(n: number | undefined): string {
  if (n == null) return "text-gray-400";
  return n >= 0 ? "text-green-400" : "text-red-400";
}
function ratioColor(v: number): string {
  if (v >= 1.5) return "text-green-400";
  if (v >= 0.5) return "text-yellow-400";
  return "text-red-400";
}

const SECTION_TABS  = ["overview", "execution"] as const;
const SECTION_LABELS: Record<string, string> = { overview: "Overview", execution: "Execution Quality" };
const ACCOUNT_TABS  = ["paper", "live", "all"] as const;
const MODE_TABS     = ["all", "scalping", "day_trading", "swing"] as const;
const MODE_LABELS: Record<string, string> = {
  all: "All Modes", scalping: "Scalping", day_trading: "Day Trading", swing: "Swing"
};
const REGIME_COLORS: Record<string, string> = {
  trending_bull:    "bg-green-500/20 text-green-300 border border-green-500/30",
  trending_bear:    "bg-red-500/20   text-red-300   border border-red-500/30",
  ranging_low_vol:  "bg-blue-500/20  text-blue-300  border border-blue-500/30",
  ranging_high_vol: "bg-orange-500/20 text-orange-300 border border-orange-500/30",
  volatile_breakout:"bg-purple-500/20 text-purple-300 border border-purple-500/30",
  quiet:            "bg-gray-500/20  text-gray-300  border border-gray-500/30",
};

// ── mini inline SVG equity curve ──────────────────────────────────────────
function EquitySparkline({ points }: { points: { time: string; equity: number }[] }) {
  if (points.length < 2) return <div className="text-gray-600 text-xs">no data</div>;
  const w = 600, h = 120, pad = 8;
  const vals = points.map((p) => p.equity);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const range = mx - mn || 1;
  const scaleX = (i: number) => pad + ((w - 2 * pad) * i) / (points.length - 1);
  const scaleY = (v: number) => h - pad - ((h - 2 * pad) * (v - mn)) / range;
  const pathD = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${scaleX(i).toFixed(1)},${scaleY(p.equity).toFixed(1)}`)
    .join(" ");
  const lastVal = vals[vals.length - 1];
  const lineColor = lastVal >= 0 ? "#4ade80" : "#f87171";
  const zeroY = scaleY(0);
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-28" preserveAspectRatio="none">
      <defs>
        <linearGradient id="eq-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={lineColor} stopOpacity="0.25" />
          <stop offset="100%" stopColor={lineColor} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      {/* Zero baseline */}
      {zeroY > pad && zeroY < h - pad && (
        <line x1={pad} y1={zeroY} x2={w - pad} y2={zeroY}
          stroke="#6b7280" strokeWidth="0.5" strokeDasharray="4 4" />
      )}
      {/* Fill area */}
      <path
        d={`${pathD} L${scaleX(points.length - 1).toFixed(1)},${h - pad} L${pad},${h - pad} Z`}
        fill="url(#eq-grad)"
      />
      {/* Line */}
      <path d={pathD} fill="none" stroke={lineColor} strokeWidth="1.5" />
    </svg>
  );
}

// ── stat card ─────────────────────────────────────────────────────────────
function StatCard({
  label, value, sub, valueClass = "text-white",
}: { label: string; value: string; sub?: string; valueClass?: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4 flex flex-col gap-1 min-w-0">
      <span className="text-xs text-gray-500 uppercase tracking-wide">{label}</span>
      <span
        className={`text-lg font-bold font-mono leading-tight break-all ${valueClass}`}
        title={value}
      >{value}</span>
      {sub && <span className="text-xs text-gray-500 truncate">{sub}</span>}
    </div>
  );
}

// ── breakdown table ───────────────────────────────────────────────────────
function BreakdownTable({
  title,
  rows,
  labelKey,
}: {
  title: string;
  rows: [string, BucketStats][];
  labelKey: string;
}) {
  if (!rows.length) return null;
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">{title}</h3>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-gray-800">
              <th className="text-left py-1 pr-3">{labelKey}</th>
              <th className="text-right px-2">Trades</th>
              <th className="text-right px-2">Win%</th>
              <th className="text-right px-2">Profit</th>
              <th className="text-right px-2">Avg/Trade</th>
             </tr>
          </thead>
          <tbody>
            {rows.map(([label, s]) => (
              <tr key={label} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                <td className="py-1.5 pr-3 text-gray-300 font-mono">{label || "—"}</td>
                <td className="text-right px-2 text-gray-400">{s.total}</td>
                <td className={`text-right px-2 font-mono ${s.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>
                  {pct(s.win_rate)}
                </td>
                <td className={`text-right px-2 font-mono ${clsProfit(s.total_profit)}`}>
                  {profit(s.total_profit)}
                </td>
                <td className={`text-right px-2 font-mono ${clsProfit(s.avg_profit)}`}>
                  {profit(s.avg_profit)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── hour-of-day heatmap ───────────────────────────────────────────────────
function HourHeatmap({ byHour }: { byHour: Record<string, BucketStats> }) {
  const hours = Array.from({ length: 24 }, (_, i) => String(i));
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Win Rate by Hour (UTC)</h3>
      <div className="grid grid-cols-12 gap-1">
        {hours.map((h) => {
          const s = byHour[h];
          const wr = s?.win_rate ?? null;
          const bg =
            wr == null ? "bg-gray-800"
            : wr >= 60  ? "bg-green-600"
            : wr >= 45  ? "bg-yellow-600"
            : "bg-red-700";
          return (
            <div
              key={h}
              className={`${bg} rounded text-center py-1 text-xs font-mono cursor-default`}
              title={s ? `${h}:00 UTC — ${s.total} trades, ${pct(wr)} win` : `${h}:00 UTC — no trades`}
            >
              <div className="text-gray-300">{h.padStart(2, "0")}</div>
              <div className="text-[10px] text-gray-200">{wr != null ? `${wr.toFixed(0)}%` : "—"}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── failure analysis panel ────────────────────────────────────────────────
function FailurePanel({ data }: { data: AnalyticsPerformance["failure_analysis"] }) {
  return (
    <div className="bg-gray-900 border border-red-900/40 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-red-400 mb-4">⚠ Failure Analysis</h3>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">

        <div>
          <p className="text-xs text-gray-500 mb-2 uppercase tracking-wide">Worst Symbols</p>
          <div className="space-y-1">
            {data.worst_symbols.length === 0 && <p className="text-xs text-gray-600">None</p>}
            {data.worst_symbols.map((s) => (
              <div key={s.symbol} className="flex justify-between text-xs">
                <span className="text-gray-300 font-mono">{s.symbol}</span>
                <span className={`font-mono ${clsProfit(s.total_profit)}`}>{profit(s.total_profit)}</span>
                <span className="text-gray-500">{s.total} trades • {pct(s.win_rate)} wr</span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <p className="text-xs text-gray-500 mb-2 uppercase tracking-wide">Worst Strategies</p>
          <div className="space-y-1">
            {data.worst_strategies.length === 0 && <p className="text-xs text-gray-600">None</p>}
            {data.worst_strategies.map((s) => (
              <div key={s.strategy} className="flex justify-between text-xs">
                <span className="text-gray-300 font-mono">{s.strategy || "—"}</span>
                <span className={`font-mono ${clsProfit(s.total_profit)}`}>{profit(s.total_profit)}</span>
                <span className="text-gray-500">{s.total} trades</span>
              </div>
            ))}
          </div>
        </div>

        <div className="md:col-span-2 flex gap-6 mt-1">
          <div className="bg-red-950/40 border border-red-800/30 rounded-lg px-4 py-3 text-center">
            <div className="text-2xl font-bold font-mono text-red-400">{data.max_losing_streak}</div>
            <div className="text-xs text-gray-500 mt-0.5">Max Loss Streak</div>
          </div>
          <div className={`border rounded-lg px-4 py-3 text-center ${
            data.current_losing_streak >= 3
              ? "bg-red-950/40 border-red-800/30"
              : "bg-gray-800/40 border-gray-700/30"
          }`}>
            <div className={`text-2xl font-bold font-mono ${
              data.current_losing_streak >= 3 ? "text-red-400" : "text-yellow-400"
            }`}>{data.current_losing_streak}</div>
            <div className="text-xs text-gray-500 mt-0.5">Current Streak</div>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── regime status pill list ───────────────────────────────────────────────
function RegimePanel({ regimes }: { regimes: Record<string, string> }) {
  const entries = Object.entries(regimes);
  if (!entries.length) return null;
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Current Market Regimes</h3>
      <div className="flex flex-wrap gap-2">
        {entries.map(([sym, label]) => (
          <div key={sym} className={`flex items-center gap-1.5 px-2 py-1 rounded-full text-xs font-mono ${
            REGIME_COLORS[label] ?? "bg-gray-700 text-gray-300"
          }`}>
            <span className="font-semibold">{sym}</span>
            <span className="opacity-70">{label.replace(/_/g, " ")}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── execution quality panel ───────────────────────────────────────────────
function ExecutionQualityPanel({ metrics }: { metrics: ExecutionQualityMetrics | null }) {
  if (!metrics || metrics.total_filled === 0) return null;

  const formatSlippage = (s: number | null) => {
    if (s == null) return "—";
    return `${s.toFixed(2)} pips`;
  };

  const formatTime = (ms: number | null) => {
    if (ms == null) return "—";
    return `${ms.toFixed(0)}ms`;
  };

  const slippageStatus = (s: number | null) => {
    if (s == null) return "text-gray-400";
    if (s < 1) return "text-green-400";
    if (s < 3) return "text-yellow-400";
    return "text-red-400";
  };

  const timeStatus = (ms: number | null) => {
    if (ms == null) return "text-gray-400";
    if (ms < 200) return "text-green-400";
    if (ms < 500) return "text-yellow-400";
    return "text-red-400";
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">Execution Quality</h3>
      
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 mb-4">
        <div>
          <div className="text-xs text-gray-500">Total Filled</div>
          <div className="text-lg font-bold text-white font-mono">{metrics.total_filled}</div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Avg Slippage</div>
          <div className={`text-lg font-bold font-mono ${slippageStatus(metrics.avg_slippage)}`}>
            {formatSlippage(metrics.avg_slippage)}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Avg Spread</div>
          <div className="text-lg font-bold font-mono text-gray-300">
            {metrics.avg_spread_pips != null ? `${metrics.avg_spread_pips.toFixed(2)} pips` : "—"}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Avg Execution Time</div>
          <div className={`text-lg font-bold font-mono ${timeStatus(metrics.avg_execution_time_ms)}`}>
            {formatTime(metrics.avg_execution_time_ms)}
          </div>
        </div>
        <div>
          <div className="text-xs text-gray-500">Slippage Coverage</div>
          <div className="text-lg font-bold text-white font-mono">
            {metrics.slippage_coverage.toFixed(1)}%
          </div>
        </div>
      </div>

      {/* By Symbol */}
      {Object.keys(metrics.by_symbol).length > 0 && (
        <div className="mt-4">
          <div className="text-xs text-gray-500 mb-2 uppercase tracking-wide">By Symbol</div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-1 pr-3">Symbol</th>
                  <th className="text-right px-2">Fills</th>
                  <th className="text-right px-2">Slippage</th>
                  <th className="text-right px-2">Spread</th>
                  <th className="text-right px-2">Exec Time</th>
                 </tr>
              </thead>
              <tbody>
                {Object.entries(metrics.by_symbol)
                  .sort((a, b) => b[1].total_filled - a[1].total_filled)
                  .map(([symbol, data]) => (
                    <tr key={symbol} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                      <td className="py-1.5 pr-3 text-gray-300 font-mono">{symbol}</td>
                      <td className="text-right px-2 text-gray-400">{data.total_filled}</td>
                      <td className={`text-right px-2 font-mono ${slippageStatus(data.avg_slippage)}`}>
                        {formatSlippage(data.avg_slippage)}
                      </td>
                      <td className="text-right px-2 font-mono text-gray-300">
                        {data.avg_spread_pips != null ? `${data.avg_spread_pips.toFixed(2)}p` : "—"}
                      </td>
                      <td className={`text-right px-2 font-mono ${timeStatus(data.avg_execution_time_ms)}`}>
                        {formatTime(data.avg_execution_time_ms)}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* By Trading Type */}
      {Object.keys(metrics.by_trading_type).length > 0 && (
        <div className="mt-4">
          <div className="text-xs text-gray-500 mb-2 uppercase tracking-wide">By Trading Mode</div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-1 pr-3">Mode</th>
                  <th className="text-right px-2">Fills</th>
                  <th className="text-right px-2">Slippage</th>
                  <th className="text-right px-2">Spread</th>
                  <th className="text-right px-2">Exec Time</th>
                 </tr>
              </thead>
              <tbody>
                {Object.entries(metrics.by_trading_type).map(([mode, data]) => (
                  <tr key={mode} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-1.5 pr-3 text-gray-300 font-mono">{mode}</td>
                    <td className="text-right px-2 text-gray-400">{data.total_filled}</td>
                    <td className={`text-right px-2 font-mono ${slippageStatus(data.avg_slippage)}`}>
                      {formatSlippage(data.avg_slippage)}
                    </td>
                    <td className="text-right px-2 font-mono text-gray-300">
                      {data.avg_spread_pips != null ? `${data.avg_spread_pips.toFixed(2)}p` : "—"}
                    </td>
                    <td className={`text-right px-2 font-mono ${timeStatus(data.avg_execution_time_ms)}`}>
                      {formatTime(data.avg_execution_time_ms)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ── main page ─────────────────────────────────────────────────────────────
export default function AnalyticsPage() {
  const { account: currentAccount } = useBotStore();
  const [sectionTab, setSectionTab] = useState<typeof SECTION_TABS[number]>("overview");
  const [accountTab, setAccountTab]  = useState<typeof ACCOUNT_TABS[number]>("paper");
  const [modeTab,    setModeTab]     = useState<typeof MODE_TABS[number]>("all");
  const [selectedAccountLogin, setSelectedAccountLogin] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<Array<{ login: number; type: string }>>([]);
  const [data,   setData]    = useState<AnalyticsPerformance | null>(null);
  const [regimes, setRegimes] = useState<Record<string, string>>({});
  const [executionQuality, setExecutionQuality] = useState<ExecutionQualityMetrics | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  // Load available accounts
  useEffect(() => {
    fetchAccounts()
      .then(res => setAccounts(res.accounts))
      .catch(() => {});
  }, []);

  const load = useCallback(async () => {
  setLoading(true);
  setError(null);
  try {
    const [perf, reg, exec] = await Promise.all([
      fetchAnalyticsPerformance(accountTab, modeTab, undefined), // limit is optional, pass undefined for default (5000)
      fetchRegimeStatus().catch(() => ({ regimes: {} })),
      fetchExecutionQuality(accountTab, modeTab).catch(() => null) // Fixed: account first, then tradingType
    ]);
    setData(perf);
    setRegimes(reg.regimes);
    setExecutionQuality(exec);
  } catch (e: unknown) {
    setError(e instanceof Error ? e.message : "Failed to load analytics");
  } finally {
    setLoading(false);
  }
}, [accountTab, modeTab]);

  useEffect(() => { load(); }, [load]);

  const byStrategyRows   = data?.by_strategy ? Object.entries(data.by_strategy).sort((a, b) => b[1].total_profit - a[1].total_profit) : [];
  const bySymbolRows     = data?.by_symbol   ? Object.entries(data.by_symbol).sort((a, b) => b[1].total_profit - a[1].total_profit) : [];
  const byModeRows       = data?.by_mode     ? Object.entries(data.by_mode).sort((a, b) => b[1].total_profit - a[1].total_profit) : [];

  const equityFinal = data?.equity_curve?.at(-1)?.equity ?? 0;

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">

      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">Analytics</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Performance metrics, ratios, and failure analysis
          </p>
        </div>
        
        {/* Account selector */}
        {accounts.length > 0 && (
          <select
            value={selectedAccountLogin ?? ""}
            onChange={(e) => setSelectedAccountLogin(e.target.value ? Number(e.target.value) : null)}
            className="px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm"
          >
            <option value="">All Accounts</option>
            {accounts.map((acc) => (
              <option key={acc.login} value={acc.login}>
                {acc.login} ({acc.type === "demo" ? "Demo" : "Live"})
              </option>
            ))}
          </select>
        )}
        
        <button
          onClick={load}
          disabled={loading}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-sm rounded-lg transition-colors"
        >
          {loading ? "Loading…" : "↻ Refresh"}
        </button>
      </div>

      {/* Section tabs */}
      <div className="flex gap-2 border-b border-gray-800 mb-2">
        {SECTION_TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setSectionTab(tab)}
            className={`px-4 py-2 font-medium text-sm transition-colors relative ${
              sectionTab === tab
                ? "text-blue-400 border-b-2 border-blue-400"
                : "text-gray-400 hover:text-gray-300"
            }`}
          >
            {SECTION_LABELS[tab]}
          </button>
        ))}
      </div>

      {/* Account tabs */}
      <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 w-fit">
        {ACCOUNT_TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setAccountTab(tab)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              accountTab === tab
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:text-white"
            }`}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </button>
        ))}
      </div>

      {/* Mode tabs */}
      <div className="flex gap-1 bg-gray-900 border border-gray-800 rounded-xl p-1 w-fit">
        {MODE_TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => setModeTab(tab)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              modeTab === tab
                ? "bg-purple-600 text-white"
                : "text-gray-400 hover:text-white"
            }`}
          >
            {MODE_LABELS[tab]}
          </button>
        ))}
      </div>

      {/* Execution Quality tab */}
      {sectionTab === "execution" && (
        <div className="mt-2">
          {executionQuality && executionQuality.total_filled > 0
            ? <ExecutionQualityPanel metrics={executionQuality} />
            : <div className="bg-gray-900 border border-gray-800 rounded-xl px-6 py-10 text-center text-gray-500">No execution quality data available</div>
          }
        </div>
      )}

      {sectionTab === "overview" && <>
        {error && (
          <div className="bg-red-950/50 border border-red-800/50 rounded-xl px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}

        {data?.message && !data.total_trades && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl px-6 py-10 text-center text-gray-500">
            {data.message}
          </div>
        )}

        {data && data.total_trades > 0 && (
          <>
            {/* Key stats */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              <StatCard label="Trades"       value={String(data.total_trades)} />
              <StatCard label="Win Rate"     value={pct(data.win_rate)}
                valueClass={data.win_rate >= 50 ? "text-green-400" : data.win_rate >= 40 ? "text-yellow-400" : "text-red-400"} />
              <StatCard label="Total P&L"    value={profit(data.total_profit)}   valueClass={clsProfit(data.total_profit)} />
              <StatCard label="Avg RR"       value={fmt(data.avg_rr)}            valueClass="text-blue-300" />
              <StatCard label="Sharpe"       value={fmt(data.sharpe_ratio)}      valueClass={ratioColor(data.sharpe_ratio)} sub="annualised" />
              <StatCard label="Sortino"      value={fmt(data.sortino_ratio)}     valueClass={ratioColor(data.sortino_ratio)} sub="annualised" />
              <StatCard label="Max Drawdown" value={`${fmt(data.max_drawdown_pct)}%`}
                valueClass={data.max_drawdown_pct > 15 ? "text-red-400" : data.max_drawdown_pct > 8 ? "text-yellow-400" : "text-green-400"} />
              <StatCard label="Avg Win"      value={profit(data.avg_win)}        valueClass="text-green-400"
                sub={`Loss: ${profit(data.avg_loss)}`} />
            </div>

            {/* Equity curve */}
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <div className="flex justify-between items-center mb-2">
                <h2 className="text-sm font-semibold text-gray-300">Equity Curve (Cumulative P&L)</h2>
                <span className={`text-sm font-mono font-semibold ${clsProfit(equityFinal)}`}>
                  {profit(equityFinal)}
                </span>
              </div>
              <EquitySparkline points={data.equity_curve} />
            </div>

            {/* Trade quality bar */}
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
              <h3 className="text-sm font-semibold text-gray-300 mb-3">Trade Quality Distribution</h3>
              <div className="flex gap-3 items-center">
                {(() => {
                  const { high, medium, low } = data.trade_quality;
                  const total = high + medium + low || 1;
                  return (
                    <>
                      <div className="flex-1 flex h-6 rounded-lg overflow-hidden">
                        {high   > 0 && <div style={{ width: `${high   / total * 100}%` }} className="bg-green-600 transition-all" title={`High confidence: ${high}`} />}
                        {medium > 0 && <div style={{ width: `${medium / total * 100}%` }} className="bg-yellow-500 transition-all" title={`Medium: ${medium}`} />}
                        {low    > 0 && <div style={{ width: `${low    / total * 100}%` }} className="bg-gray-600 transition-all" title={`Low / unknown: ${low}`} />}
                      </div>
                      <div className="flex gap-3 text-xs whitespace-nowrap">
                        <span className="text-green-400">● High {high}</span>
                        <span className="text-yellow-400">● Med {medium}</span>
                        <span className="text-gray-400">● Low {low}</span>
                      </div>
                    </>
                  );
                })()}
              </div>
            </div>

            {/* Regime status */}
            {Object.keys(regimes).length > 0 && <RegimePanel regimes={regimes} />}

            {/* Breakdown tables */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <BreakdownTable
                title="By Strategy"
                rows={byStrategyRows}
                labelKey="Strategy"
              />
              <BreakdownTable
                title="By Symbol"
                rows={bySymbolRows}
                labelKey="Symbol"
              />
              {modeTab === "all" && (
                <BreakdownTable
                  title="By Trading Mode"
                  rows={byModeRows}
                  labelKey="Mode"
                />
              )}
              {accountTab === "all" && (
                <BreakdownTable
                  title="By Account"
                  rows={data ? Object.entries(data.by_account).sort((a, b) => b[1].total_profit - a[1].total_profit) : []}
                  labelKey="Account"
                />
              )}
            </div>

            {/* Hour heatmap */}
            <HourHeatmap byHour={data.by_hour} />

            {/* Failure analysis */}
            <FailurePanel data={data.failure_analysis} />
          </>
        )}
      </>}
    </div>
  );
}