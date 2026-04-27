"use client";

import { useState, useEffect, useCallback } from "react";
import {
  runBacktest,
  fetchBacktestStrategies,
  fetchBacktestHistory,
  fetchBacktestRun,
  deleteBacktestRun,
  fetchAvailableSymbols,
  BacktestRequest,
} from "@/lib/api";

// ── Types ─────────────────────────────────────────────────────────────────
type TradingType = "scalping" | "day_trading" | "swing";

interface HistoryItem {
  id: string; run_at: string; symbol: string; strategy: string;
  trading_type: string; timeframe: string; bars_tested: number;
  total_trades: number; win_rate: number; profit_factor: number;
  max_drawdown_pct: number; sharpe_ratio: number; total_pnl_pct: number;
  initial_balance: number; risk_pct: number;
}

interface HistoryPage {
  total: number; page: number; page_size: number; pages: number;
  items: HistoryItem[];
}

interface BacktestTrade {
  trade_num:    number;
  entry_time:   string;
  exit_time:    string;
  direction:    string;
  entry_price:  number;
  exit_price:   number;
  sl_price:     number;
  tp_price:     number;
  outcome:      string;
  rr:           number;
  pnl_pct:      number;
  equity:       number;
}

interface BacktestResult {
  symbol:           string;
  strategy:         string;
  trading_type:     string;
  timeframe:        string;
  bars_tested:      number;
  initial_balance:  number;
  trades:           BacktestTrade[];
  equity_curve:     { time: string; equity: number }[];
  total_trades:     number;
  win_rate:         number;
  profit_factor:    number;
  max_drawdown_pct: number;
  sharpe_ratio:     number;
  total_pnl_pct:    number;
  avg_rr:           number;
  best_trade_pct:   number;
  worst_trade_pct:  number;
  avg_trade_pct:    number;
  expectancy_pct:   number;
}

// ── Symbol groups — static fallback used before API responds ─────────────
const FALLBACK_SYMBOLS = [
  "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD",
  "EURJPY", "GBPJPY", "EURGBP",
  "GOLD", "SILVER", "OilCash", "BRENTCash", "NGASCash",
  "US100Cash", "US30Cash", "US500Cash", "GER40Cash", "UK100Cash",
  "BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD",
  "Tesla", "Nvidia", "Apple", "Microsoft", "Amazon", "Google", "Facebook",
];

// ── Helpers ───────────────────────────────────────────────────────────────
const MODES: TradingType[] = ["scalping", "day_trading", "swing"];
const DEFAULT_STRATEGIES: Record<TradingType, string[]> = {
  scalping:    ["ema_scalp", "bb_squeeze", "vwap_reversion", "stoch_rsi_pullback"],
  day_trading: ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
  swing:       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
};

function pct(v: number) { return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`; }
function modeLabel(m: string) { return m.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }
function relTime(iso: string) {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const m = Math.floor(diff / 60_000);
    if (m < 1) return "just now";
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  } catch { return iso; }
}
function fmtTime(iso: string) {
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }); }
  catch { return iso; }
}

// ── Mini sparkline for history rows ──────────────────────────────────────
function MiniSparkline({ pnl }: { pnl: number }) {
  const color = pnl >= 0 ? "#10b981" : "#ef4444";
  const end = pnl >= 0 ? 4 : 24;
  return (
    <svg viewBox="0 0 40 28" width="40" height="28">
      <polyline points={`2,14 20,14 38,${end}`} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

// ── Equity SVG sparkline ───────────────────────────────────────────────────
function EquityCurve({ points, initial }: { points: { time: string; equity: number }[]; initial: number }) {
  if (points.length < 2) return <p className="text-sm text-gray-600 py-4">Not enough trades to draw curve.</p>;

  const H = 180;
  const W = 700;
  const all  = [initial, ...points.map((p) => p.equity)];
  const minE = Math.min(...all);
  const maxE = Math.max(...all);
  const range = maxE - minE || 1;

  const xOf = (i: number) => (i / (points.length)) * W;
  const yOf = (v: number) => H - ((v - minE) / range) * H;

  const coords = [
    `0,${yOf(initial)}`,
    ...points.map((p, i) => `${xOf(i + 1)},${yOf(p.equity)}`),
  ].join(" ");

  const fillCoords = `0,${H} ${coords} ${W},${H}`;
  const isUp = points[points.length - 1].equity >= initial;

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }}>
      <defs>
        <linearGradient id="eq_grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={isUp ? "#10b981" : "#ef4444"} stopOpacity="0.3" />
          <stop offset="100%" stopColor={isUp ? "#10b981" : "#ef4444"} stopOpacity="0" />
        </linearGradient>
      </defs>
      {/* zero-profit reference line */}
      <line x1="0" y1={yOf(initial)} x2={W} y2={yOf(initial)}
        stroke="#4b5563" strokeWidth="1" strokeDasharray="4 4" />
      <polyline points={fillCoords} fill="url(#eq_grad)" stroke="none" />
      <polyline points={coords} fill="none"
        stroke={isUp ? "#10b981" : "#ef4444"} strokeWidth="2" strokeLinejoin="round" />
    </svg>
  );
}

// ── Stat card ────────────────────────────────────────────────────────────
function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
      <p className="text-xs text-gray-500 mb-1">{label}</p>
      <p className={`text-xl font-bold ${color ?? "text-white"}`}>{value}</p>
    </div>
  );
}

// ── Compare / Diff shared config ──────────────────────────────────────────
type CompareEntry = BacktestResult | { error: string };

const COMPARE_METRICS: {
  key: string;
  label: string;
  fmt: (v: number) => string;
  higherBetter: boolean;
}[] = [
  { key: "total_pnl_pct",    label: "Return",        fmt: pct,                                higherBetter: true  },
  { key: "win_rate",         label: "Win Rate",       fmt: (v) => `${(v*100).toFixed(1)}%`,   higherBetter: true  },
  { key: "profit_factor",    label: "Profit Factor",  fmt: (v) => v.toFixed(2),               higherBetter: true  },
  { key: "max_drawdown_pct", label: "Max Drawdown",   fmt: (v) => `-${v.toFixed(2)}%`,        higherBetter: false },
  { key: "sharpe_ratio",     label: "Sharpe Ratio",   fmt: (v) => v.toFixed(2),               higherBetter: true  },
  { key: "avg_rr",           label: "Avg RR",         fmt: (v) => v.toFixed(2),               higherBetter: true  },
  { key: "total_trades",     label: "Trades",         fmt: (v) => v.toString(),               higherBetter: true  },
  { key: "expectancy_pct",   label: "Expectancy",     fmt: pct,                               higherBetter: true  },
];

// ── Strategy comparison table ─────────────────────────────────────────────
function CompareTable({
  results,
  runningStrat,
  onClear,
}: {
  results: Record<string, CompareEntry | null>;
  runningStrat: string | null;
  onClear?: () => void;  // GAP-BT-3
}) {
  const strats = Object.keys(results);
  if (strats.length === 0) return null;
  return (
    <section className="bg-gray-900 border border-gray-800 rounded-xl p-5 overflow-x-auto">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-sm font-semibold text-white">Strategy Comparison</h2>
        {onClear && !runningStrat && (
          <button onClick={onClear} className="text-xs text-gray-500 hover:text-white transition-colors">
            ✕ Clear
          </button>
        )}
      </div>
      <table className="w-full text-xs border-collapse" style={{ minWidth: 480 }}>
        <thead>
          <tr className="border-b border-gray-800">
            <th className="text-left text-gray-500 pb-2 pr-4 font-medium w-28">Metric</th>
            {strats.map((s) => (
              <th key={s} className="text-left text-gray-300 pb-2 pr-4 font-medium">
                {s}
                {runningStrat === s && <span className="ml-1 text-blue-400">●</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {COMPARE_METRICS.map((m) => {
            const values = strats.map((s) => {
              const r = results[s];
              return r && !("error" in r) ? (r as unknown as Record<string, number>)[m.key] : null;
            });
            const valid = values.filter((v): v is number => v !== null);
            const best = valid.length > 1
              ? (m.higherBetter ? Math.max(...valid) : Math.min(...valid))
              : null;
            return (
              <tr key={m.key} className="border-b border-gray-800/40">
                <td className="py-2 pr-4 text-gray-500">{m.label}</td>
                {strats.map((s) => {
                  const r = results[s];
                  if (!r) return (
                    <td key={s} className="py-2 pr-4">
                      {runningStrat === s
                        ? <span className="text-blue-400">running…</span>
                        : <span className="text-gray-700">—</span>}
                    </td>
                  );
                  if ("error" in r) return (
                    <td key={s} className="py-2 pr-4 text-red-400 text-[10px] max-w-30 truncate" title={r.error}>
                      {r.error}
                    </td>
                  );
                  const val = (r as unknown as Record<string, number>)[m.key];
                  const isBest = best !== null && val === best;
                  const color =
                    (m.key === "total_pnl_pct" || m.key === "expectancy_pct")
                      ? (val >= 0 ? "text-emerald-400" : "text-red-400")
                      : m.key === "max_drawdown_pct" ? "text-red-400"
                      : m.key === "win_rate" ? (val >= 0.5 ? "text-emerald-400" : "text-amber-400")
                      : "text-white";
                  return (
                    <td key={s} className={`py-2 pr-4 font-medium ${color}`}>
                      {m.fmt(val)}
                      {isBest && <span className="ml-1 text-yellow-400">★</span>}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

// ── Before / After diff panel ─────────────────────────────────────────────
function DiffPanel({
  current,
  baseline,
  onClear,
}: {
  current: BacktestResult;
  baseline: BacktestResult & { _label?: string };
  onClear?: () => void;  // GAP-BT-3
}) {
  const label = baseline._label ?? `${baseline.strategy} / ${baseline.symbol} / ${baseline.timeframe}`;

  const fmtDelta = (key: string, delta: number): string => {
    const sign = delta >= 0 ? "+" : "";
    if (key === "total_trades")  return `${sign}${Math.round(delta)}`;
    if (key === "win_rate")      return `${sign}${(delta * 100).toFixed(1)}pp`;
    if (key === "total_pnl_pct" || key === "expectancy_pct") return `${sign}${delta.toFixed(2)}pp`;
    return `${sign}${delta.toFixed(2)}`;
  };

  return (
    <section className="bg-gray-900 border border-blue-900/40 rounded-xl p-5">
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-sm font-semibold text-white">Before / After Diff</h2>
        <span className="text-xs text-gray-500">Baseline: {label}</span>
        {onClear && (
          <button onClick={onClear} className="ml-auto text-xs text-gray-500 hover:text-white transition-colors">
            ✕ Clear Baseline
          </button>
        )}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse" style={{ minWidth: 420 }}>
          <thead>
            <tr className="border-b border-gray-800">
              <th className="text-left text-gray-500 pb-2 pr-4 font-medium">Metric</th>
              <th className="text-left text-gray-500 pb-2 pr-4 font-medium">Baseline</th>
              <th className="text-left text-gray-500 pb-2 pr-4 font-medium">Current</th>
              <th className="text-left text-gray-500 pb-2 font-medium">Change</th>
            </tr>
          </thead>
          <tbody>
            {COMPARE_METRICS.map((m) => {
              const bVal = (baseline as unknown as Record<string, number>)[m.key];
              const cVal = (current  as unknown as Record<string, number>)[m.key];
              const delta = cVal - bVal;
              const noChange = Math.abs(delta) < 0.0001;
              const isImproved = !noChange && (m.higherBetter ? delta > 0 : delta < 0);
              return (
                <tr key={m.key} className="border-b border-gray-800/40">
                  <td className="py-2 pr-4 text-gray-500">{m.label}</td>
                  <td className="py-2 pr-4 text-gray-400">{m.fmt(bVal)}</td>
                  <td className="py-2 pr-4 text-white font-medium">{m.fmt(cVal)}</td>
                  <td className={`py-2 font-semibold ${
                    noChange ? "text-gray-600" : isImproved ? "text-emerald-400" : "text-red-400"
                  }`}>
                    {noChange ? "—" : (isImproved ? "▲ " : "▼ ") + fmtDelta(m.key, Math.abs(delta))}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────
export default function BacktestPage() {
  const [strategies, setStrategies] = useState<Record<TradingType, string[]>>(DEFAULT_STRATEGIES);
  const [symbolOptions, setSymbolOptions] = useState<string[]>(FALLBACK_SYMBOLS);
  const [symInput,      setSymInput]      = useState("EURUSD");
  const [symOpen,       setSymOpen]       = useState(false);

  // Form state
  const [mode,    setMode]    = useState<TradingType>("scalping");
  const [symbol,  setSymbol]  = useState("EURUSD");
  const [strat,   setStrat]   = useState("ema_scalp");
  const [bars,    setBars]    = useState(2000);
  const [balance, setBalance] = useState(10000);
  const [riskPct, setRiskPct] = useState(1.0);
  const [useAiFilters, setUseAiFilters] = useState(true);  // GAP-BT-2

  const [running, setRunning] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [result,  setResult]  = useState<BacktestResult | null>(null);

  // History state
  const [history,     setHistory]     = useState<HistoryPage | null>(null);
  const [historyPage, setHistoryPage] = useState(1);
  const [histLoading, setHistLoading] = useState(false);
  const [deletingId,  setDeletingId]  = useState<string | null>(null);
  const [loadingId,   setLoadingId]   = useState<string | null>(null);

  // Compare + diff state
  const [compareResults,  setCompareResults]  = useState<Record<string, CompareEntry | null>>({});
  const [comparing,       setComparing]       = useState(false);
  const [comparingStrat,  setComparingStrat]  = useState<string | null>(null);
  const [baseline,        setBaseline]        = useState<(BacktestResult & { _label?: string }) | null>(null);

  const loadHistory = useCallback(async (page = 1) => {
    setHistLoading(true);
    try {
      const data = await fetchBacktestHistory({ page, page_size: 15 });
      setHistory(data as HistoryPage);
      setHistoryPage(page);
    } catch { /* offline */ }
    finally { setHistLoading(false); }
  }, []);

  // Load strategies, available symbols, and history on mount
  useEffect(() => {
    fetchBacktestStrategies()
      .then((data) => setStrategies(data as Record<TradingType, string[]>))
      .catch(() => {});
    fetchAvailableSymbols()
      .then((data) => { if (data.symbols.length > 0) setSymbolOptions(data.symbols); })
      .catch(() => {});
    loadHistory(1);
  }, [loadHistory]);

  const handleModeChange = (m: TradingType) => {
    setMode(m);
    setSymbol("EURUSD");
    setSymInput("EURUSD");
    setSymOpen(false);
    setStrat(strategies[m]?.[0] ?? "");
    setCompareResults({});  // BUG-BT-3: clear stale compare from previous mode
    setBaseline(null);      // BUG-BT-3: clear stale baseline from previous mode
  };

  async function handleRun() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const req: BacktestRequest = { symbol, strategy: strat, trading_type: mode, bars, initial_balance: balance, risk_pct: riskPct, use_ai_filters: useAiFilters };
      const data = await runBacktest(req);
      setResult(data as BacktestResult);
      loadHistory(1);
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const msg = Array.isArray(detail)
        ? detail.map((d: { msg?: string; loc?: string[] }) => `${d.loc?.slice(-1)[0] ?? "field"}: ${d.msg ?? d}`).join("; ")
        : (typeof detail === "string" ? detail : (e as { message?: string })?.message ?? "Unknown error");
      setError(String(msg));
    } finally { setRunning(false); }
  }

  async function handleLoadRun(id: string) {
    setLoadingId(id);
    try {
      const data = await fetchBacktestRun(id);
      setResult(data as BacktestResult);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch { /* ignore */ }
    finally { setLoadingId(null); }
  }

  async function handleDelete(id: string) {
    setDeletingId(id);
    try {
      await deleteBacktestRun(id);
      if ((result as BacktestResult & { id?: string })?.id === id) setResult(null);
      loadHistory(historyPage);
    } catch { /* ignore */ }
    finally { setDeletingId(null); }
  }

  async function handleCompare() {
    const stratList = strategies[mode] ?? [];
    if (stratList.length === 0) return;
    setComparing(true);
    setCompareResults(Object.fromEntries(stratList.map((s) => [s, null])));
    for (const s of stratList) {
      setComparingStrat(s);
      try {
        const req: BacktestRequest = { symbol, strategy: s, trading_type: mode, bars, initial_balance: balance, risk_pct: riskPct, use_ai_filters: useAiFilters };
        const data = await runBacktest(req) as BacktestResult;
        setCompareResults((prev) => ({ ...prev, [s]: data }));
      } catch (e) {
        const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
        const msg = Array.isArray(detail)
          ? detail.map((d: { msg?: string }) => d.msg ?? "error").join("; ")
          : typeof detail === "string" ? detail : (e as { message?: string })?.message ?? "Failed";
        setCompareResults((prev) => ({ ...prev, [s]: { error: msg } }));
      }
    }
    setComparingStrat(null);
    setComparing(false);
    loadHistory(1);
  }

  function handleSetBaseline() {
    if (!result) return;
    setBaseline({ ...result, _label: `${result.strategy} / ${result.symbol} / ${result.timeframe}` });
  }

  const outcomeColor = (o: string) =>
    o === "tp_hit" ? "text-emerald-400" : o === "sl_hit" ? "text-red-400" : "text-amber-400";

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">
      <div>
        <h1 className="text-2xl font-bold text-white">Strategy Backtest</h1>
        <p className="text-sm text-gray-500 mt-1">
          Walk-forward simulation on live MT5 historical data — no look-ahead bias.
        </p>
      </div>

      {/* ── Config form ───────────────────────────────────────────────── */}
      <section className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h2 className="text-base font-semibold text-white mb-5">Configuration</h2>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
          {/* Mode — first so symbol resets relative to it */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Mode</label>
            <select
              value={mode}
              onChange={(e) => handleModeChange(e.target.value as TradingType)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            >
              {MODES.map((m) => (
                <option key={m} value={m}>{modeLabel(m)}</option>
              ))}
            </select>
          </div>

          {/* Symbol — searchable, all live MT5 symbols */}
          <div className="flex flex-col gap-1 col-span-2 md:col-span-1">
            <label className="text-xs text-gray-500">
              Symbol
              {symbolOptions.length > 0 && (
                <span className="ml-1 text-gray-600">({symbolOptions.length} available)</span>
              )}
            </label>
            <div className="relative">
              <input
                type="text"
                autoComplete="off"
                value={symInput}
                onChange={(e) => {
                  setSymInput(e.target.value.toUpperCase());
                  setSymOpen(true);
                }}
                onFocus={() => setSymOpen(true)}
                onBlur={() => {
                  // commit on exact match (case-insensitive), else revert to last valid symbol
                  const match = symbolOptions.find(
                    (s) => s.toUpperCase() === symInput.toUpperCase()
                  );
                  if (match) { setSymbol(match); setSymInput(match); }
                  else { setSymInput(symbol); }
                  setSymOpen(false);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    const match = symbolOptions.find(
                      (s) => s.toUpperCase() === symInput.toUpperCase()
                    );
                    if (match) { setSymbol(match); setSymInput(match); }
                    setSymOpen(false);
                  } else if (e.key === "Escape") {
                    setSymInput(symbol);
                    setSymOpen(false);
                  }
                }}
                placeholder="Search symbols…"
                className="w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
              />
              {symOpen && (() => {
                const matches = symInput.trim()
                  ? symbolOptions.filter((s) => s.toUpperCase().includes(symInput.toUpperCase()))
                  : symbolOptions;
                return matches.length > 0 ? (
                  <ul className="absolute z-50 mt-1 w-full max-h-48 overflow-y-auto bg-gray-800 border border-gray-700 rounded shadow-lg">
                    {matches.slice(0, 50).map((s) => (
                      <li
                        key={s}
                        onMouseDown={(e) => {
                          e.preventDefault(); // keep focus so blur doesn't fire first
                          setSymbol(s);
                          setSymInput(s);
                          setSymOpen(false);
                        }}
                        className={`px-3 py-1.5 text-sm cursor-pointer hover:bg-gray-700 ${
                          s === symbol ? "text-blue-400 font-medium" : "text-white"
                        }`}
                      >
                        {s}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <ul className="absolute z-50 mt-1 w-full bg-gray-800 border border-gray-700 rounded shadow-lg">
                    <li className="px-3 py-1.5 text-sm text-gray-500">No match</li>
                  </ul>
                );
              })()}
            </div>
          </div>

          {/* Strategy */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Strategy</label>
            <select
              value={strat}
              onChange={(e) => setStrat(e.target.value)}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            >
              {(strategies[mode] ?? []).map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </div>

          {/* Bars */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Bars</label>
            <input
              type="number"
              value={bars}
              min={200}
              max={50000}
              onChange={(e) => setBars(Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Balance */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Initial Balance ($)</label>
            <input
              type="number"
              value={balance}
              min={100}
              onChange={(e) => setBalance(Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* Risk % */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Risk per Trade (%)</label>
            <input
              type="number"
              value={riskPct}
              min={0.1}
              max={10}
              step={0.1}
              onChange={(e) => setRiskPct(Number(e.target.value))}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
            />
          </div>

          {/* AI Filters — GAP-BT-2 */}
          <div className="flex flex-col gap-1 justify-end">
            <label className="text-xs text-gray-500">AI Filters</label>
            <label className="flex items-center gap-2 cursor-pointer select-none">
              <input
                type="checkbox"
                checked={useAiFilters}
                onChange={(e) => setUseAiFilters(e.target.checked)}
                className="w-4 h-4 accent-blue-500"
              />
              <span className="text-sm text-gray-300">LSTM + RL gate</span>
            </label>
          </div>
        {!useAiFilters && (
          <p className="mt-3 text-xs text-amber-500/80">
            ⚠ AI filters off — all strategy signals will be simulated without LSTM or RL gate. Results will not match live execution.
          </p>
        )}
          <button
            disabled={running || comparing}
            onClick={handleRun}
            className="px-6 py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-lg text-sm font-semibold text-white transition-colors"
          >
            {running ? "Running simulation…" : "▶  Run Backtest"}
          </button>
          <button
            disabled={running || comparing}
            onClick={handleCompare}
            className="px-5 py-2.5 bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded-lg text-sm font-semibold text-white transition-colors"
          >
            {comparing
              ? `⊞  Comparing ${comparingStrat ?? ""}…`
              : "⊞  Compare All Strategies"}
          </button>
        </div>

        {error && (
          <p className="mt-3 text-sm text-red-400 bg-red-950/40 border border-red-800 rounded px-3 py-2">
            {error}
          </p>
        )}
      </section>

      {/* ── Compare table ─────────────────────────────────────────────────── */}
      {Object.keys(compareResults).length > 0 && (
        <CompareTable
          results={compareResults}
          runningStrat={comparingStrat}
          onClear={() => setCompareResults({})}  // GAP-BT-3
        />
      )}

      {result && (
        <>
          {/* ── Summary header ────────────────────────────────────────── */}
          <div className="flex flex-wrap items-center gap-2 text-sm text-gray-400">
            <span className="text-white font-semibold text-base">{result.symbol}</span>
            <span className="bg-gray-800 px-2 py-0.5 rounded">{result.strategy}</span>
            <span className="bg-gray-800 px-2 py-0.5 rounded">{modeLabel(result.trading_type)}</span>
            <span className="bg-gray-800 px-2 py-0.5 rounded">{result.timeframe}</span>
            <span>{result.bars_tested.toLocaleString()} bars •</span>
            <span>{result.total_trades} trades</span>
            {(result as BacktestResult & { run_at?: string }).run_at && (
              <span className="text-gray-600 text-xs">• {relTime((result as BacktestResult & { run_at?: string }).run_at!)}</span>
            )}            <button
              onClick={handleSetBaseline}
              className="ml-auto text-xs px-3 py-1 bg-gray-800 hover:bg-gray-700 border border-gray-700 rounded text-gray-400 hover:text-white transition-colors"
            >
              {baseline ? "↺ Update Baseline" : "📌 Pin as Baseline"}
            </button>          </div>

          {/* ── Stat cards ─────────────────────────────────────────────── */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-5 gap-4">
            <Stat
              label="Total Return"
              value={pct(result.total_pnl_pct)}
              color={result.total_pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}
            />
            <Stat
              label="Win Rate"
              value={`${(result.win_rate * 100).toFixed(1)}%`}
              color={result.win_rate >= 0.5 ? "text-emerald-400" : "text-amber-400"}
            />
            <Stat label="Profit Factor" value={result.profit_factor.toFixed(2)} />
            <Stat
              label="Max Drawdown"
              value={`-${result.max_drawdown_pct.toFixed(2)}%`}
              color="text-red-400"
            />
            <Stat label="Sharpe Ratio" value={result.sharpe_ratio.toFixed(2)} />
            <Stat label="Avg RR" value={result.avg_rr.toFixed(2)} />
            <Stat
              label="Best Trade"
              value={pct(result.best_trade_pct)}
              color="text-emerald-400"
            />
            <Stat
              label="Worst Trade"
              value={pct(result.worst_trade_pct)}
              color="text-red-400"
            />
            <Stat label="Avg Trade" value={pct(result.avg_trade_pct)} />
            <Stat
              label="Expectancy"
              value={pct(result.expectancy_pct)}
              color={result.expectancy_pct >= 0 ? "text-emerald-400" : "text-red-400"}
            />
          </div>
          {/* ── Before/After diff ─────────────────────────────────────────────────── */}
          {baseline && (
            <DiffPanel current={result} baseline={baseline} onClear={() => setBaseline(null)} />  // GAP-BT-3
          )}
          {/* ── Equity curve ───────────────────────────────────────────── */}
          <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h2 className="text-sm font-semibold text-white mb-3">Equity Curve</h2>
            <EquityCurve points={result.equity_curve} initial={result.initial_balance} />
            <div className="flex justify-between text-xs text-gray-500 mt-2">
              <span>${result.initial_balance.toLocaleString()}</span>
              <span className={result.equity_curve.length > 0
                ? (result.equity_curve[result.equity_curve.length - 1].equity >= result.initial_balance
                  ? "text-emerald-400" : "text-red-400")
                : ""}>
                ${result.equity_curve.length > 0
                  ? result.equity_curve[result.equity_curve.length - 1].equity.toLocaleString(undefined, { maximumFractionDigits: 0 })
                  : result.initial_balance.toLocaleString()}
              </span>
            </div>
          </section>

          {/* ── Trade log ─────────────────────────────────────────────── */}
          <section>
            <h2 className="text-sm font-semibold text-white mb-3">
              Trade Log <span className="text-gray-500 font-normal">({result.trades.length} trades)</span>
            </h2>
            {result.trades.length === 0 ? (
              <p className="text-sm text-gray-600">No trades fired in this period.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs border-collapse">
                  <thead>
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <th className="pb-2 pr-3">#</th>
                      <th className="pb-2 pr-3">Entry Time</th>
                      <th className="pb-2 pr-3">Exit Time</th>
                      <th className="pb-2 pr-3">Dir</th>
                      <th className="pb-2 pr-3">Entry</th>
                      <th className="pb-2 pr-3">Exit</th>
                      <th className="pb-2 pr-3">SL</th>
                      <th className="pb-2 pr-3">TP</th>
                      <th className="pb-2 pr-3">Outcome</th>
                      <th className="pb-2 pr-3">RR</th>
                      <th className="pb-2 pr-3">P&L %</th>
                      <th className="pb-2">Equity</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((t) => (
                      <tr
                        key={t.trade_num}
                        className="border-b border-gray-800/40 hover:bg-gray-900/50"
                      >
                        <td className="py-1.5 pr-3 text-gray-500">{t.trade_num}</td>
                        <td className="py-1.5 pr-3 text-gray-400">{fmtTime(t.entry_time)}</td>
                        <td className="py-1.5 pr-3 text-gray-400">{fmtTime(t.exit_time)}</td>
                        <td className={`py-1.5 pr-3 font-semibold ${t.direction === "BUY" ? "text-blue-400" : "text-orange-400"}`}>
                          {t.direction}
                        </td>
                        <td className="py-1.5 pr-3">{t.entry_price}</td>
                        <td className="py-1.5 pr-3">{t.exit_price}</td>
                        <td className="py-1.5 pr-3 text-red-500">{t.sl_price}</td>
                        <td className="py-1.5 pr-3 text-emerald-600">{t.tp_price}</td>
                        <td className={`py-1.5 pr-3 font-medium ${outcomeColor(t.outcome)}`}>
                          {t.outcome.replace("_", " ")}
                        </td>
                        <td className="py-1.5 pr-3">{t.rr.toFixed(2)}</td>
                        <td className={`py-1.5 pr-3 font-medium ${t.pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                          {pct(t.pnl_pct)}
                        </td>
                        <td className="py-1.5 font-medium text-white">
                          ${t.equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      )}

      {/* ── History panel ───────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-white">
            Backtest History
            {history && <span className="ml-2 text-sm text-gray-500 font-normal">({history.total} saved)</span>}
          </h2>
          <button onClick={() => loadHistory(historyPage)}
            className="text-xs text-gray-500 hover:text-white transition-colors">
            {histLoading ? "Loading…" : "↻ Refresh"}
          </button>
        </div>

        {!history || history.items.length === 0 ? (
          <p className="text-sm text-gray-600">No runs saved yet — run a backtest above.</p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-xs border-collapse">
                <thead>
                  <tr className="text-left text-gray-500 border-b border-gray-800">
                    {["","Symbol","Strategy","Mode","TF","Bars","Trades","Win %","P-Factor","Max DD","Return","Run",""].map((h, i) => (
                      <th key={i} className="pb-2 pr-3 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {history.items.map((item) => {
                    const isActive = (result as BacktestResult & { id?: string })?.id === item.id;
                    return (
                      <tr key={item.id}
                        onClick={() => handleLoadRun(item.id)}
                        className={`border-b border-gray-800/40 hover:bg-gray-900/50 cursor-pointer ${
                          isActive ? "bg-blue-950/30" : ""
                        }`}>
                        <td className="py-2 pr-2"><MiniSparkline pnl={item.total_pnl_pct} /></td>
                        <td className="py-2 pr-3 font-semibold text-white">{item.symbol}</td>
                        <td className="py-2 pr-3 text-gray-400">{item.strategy}</td>
                        <td className="py-2 pr-3 text-gray-400">{modeLabel(item.trading_type)}</td>
                        <td className="py-2 pr-3">{item.timeframe}</td>
                        <td className="py-2 pr-3">{item.bars_tested.toLocaleString()}</td>
                        <td className="py-2 pr-3">{item.total_trades}</td>
                        <td className={`py-2 pr-3 font-medium ${item.win_rate >= 0.5 ? "text-emerald-400" : "text-amber-400"}`}>
                          {(item.win_rate * 100).toFixed(1)}%
                        </td>
                        <td className="py-2 pr-3">{item.profit_factor.toFixed(2)}</td>
                        <td className="py-2 pr-3 text-red-400">-{item.max_drawdown_pct.toFixed(1)}%</td>
                        <td className={`py-2 pr-3 font-semibold ${item.total_pnl_pct >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                          {pct(item.total_pnl_pct)}
                        </td>
                        <td className="py-2 pr-3 text-gray-600">{relTime(item.run_at)}</td>
                        <td className="py-2">
                          <button
                            onClick={(e) => { e.stopPropagation(); handleDelete(item.id); }}
                            disabled={deletingId === item.id}
                            className="px-2 py-0.5 text-xs text-red-500 hover:text-red-400 hover:bg-red-950/40 rounded disabled:opacity-40 transition-colors">
                            {deletingId === item.id ? "…" : "✕"}
                          </button>
                          {loadingId === item.id && <span className="ml-1 text-gray-600 text-[10px]">loading…</span>}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {history.pages > 1 && (
              <div className="flex items-center gap-2 mt-4 text-sm">
                <button disabled={historyPage <= 1} onClick={() => loadHistory(historyPage - 1)}
                  className="px-3 py-1 bg-gray-800 rounded disabled:opacity-40 hover:bg-gray-700">← Prev</button>
                <span className="text-gray-500">Page {historyPage} / {history.pages}</span>
                <button disabled={historyPage >= history.pages} onClick={() => loadHistory(historyPage + 1)}
                  className="px-3 py-1 bg-gray-800 rounded disabled:opacity-40 hover:bg-gray-700">Next →</button>
                <span className="text-gray-600 text-xs ml-2">{history.total} total runs</span>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
