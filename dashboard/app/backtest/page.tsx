"use client";

import { useState, useEffect, useCallback } from "react";
import {
  runBacktest,
  fetchBacktestStrategies,
  BacktestRequest,
} from "@/lib/api";

// ── Types ─────────────────────────────────────────────────────────────────
type TradingType = "scalping" | "day_trading" | "swing";

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

// ── Helpers ───────────────────────────────────────────────────────────────
const MODES: TradingType[] = ["scalping", "day_trading", "swing"];
const DEFAULT_STRATEGIES: Record<TradingType, string[]> = {
  scalping:    ["ema_scalp", "bb_squeeze", "vwap_reversion"],
  day_trading: ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
  swing:       ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
};

function pct(v: number) { return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`; }
function fmtTime(iso: string) {
  try { return new Date(iso).toLocaleString(undefined, { dateStyle: "short", timeStyle: "short" }); }
  catch { return iso; }
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

// ── Page ──────────────────────────────────────────────────────────────────
export default function BacktestPage() {
  const [strategies, setStrategies] = useState<Record<TradingType, string[]>>(DEFAULT_STRATEGIES);

  // Form state
  const [symbol,  setSymbol]  = useState("EURUSD");
  const [mode,    setMode]    = useState<TradingType>("scalping");
  const [strat,   setStrat]   = useState("ema_scalp");
  const [bars,    setBars]    = useState(2000);
  const [balance, setBalance] = useState(10000);
  const [riskPct, setRiskPct] = useState(1.0);

  const [running, setRunning] = useState(false);
  const [error,   setError]   = useState<string | null>(null);
  const [result,  setResult]  = useState<BacktestResult | null>(null);

  // Load strategy list on mount
  useEffect(() => {
    fetchBacktestStrategies()
      .then((data) => setStrategies(data as Record<TradingType, string[]>))
      .catch(() => {/* use defaults */});
  }, []);

  // When mode changes, reset strategy to first option
  const handleModeChange = useCallback((m: TradingType) => {
    setMode(m);
    setStrat(strategies[m]?.[0] ?? "");
  }, [strategies]);

  async function handleRun() {
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const req: BacktestRequest = {
        symbol,
        strategy: strat,
        trading_type: mode,
        bars,
        initial_balance: balance,
        risk_pct: riskPct,
      };
      const data = await runBacktest(req);
      setResult(data as BacktestResult);
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } }; message?: string })
        ?.response?.data?.detail ?? (e as { message?: string })?.message ?? "Unknown error";
      setError(String(msg));
    } finally {
      setRunning(false);
    }
  }

  const modeLabel = (m: string) => m.replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const outcomeColor = (o: string) =>
    o === "tp_hit" ? "text-emerald-400" : o === "sl_hit" ? "text-red-400" : "text-amber-400";

  return (
    <main className="min-h-screen bg-gray-950 text-gray-200 p-6 space-y-8">
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
          {/* Symbol */}
          <div className="flex flex-col gap-1">
            <label className="text-xs text-gray-500">Symbol</label>
            <input
              value={symbol}
              onChange={(e) => setSymbol(e.target.value.toUpperCase())}
              className="bg-gray-800 border border-gray-700 rounded px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
              placeholder="EURUSD"
            />
          </div>

          {/* Mode */}
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
              max={10000}
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
        </div>

        <button
          disabled={running}
          onClick={handleRun}
          className="mt-5 px-6 py-2.5 bg-blue-600 hover:bg-blue-500 disabled:opacity-50 rounded-lg text-sm font-semibold text-white transition-colors"
        >
          {running ? "Running simulation…" : "▶  Run Backtest"}
        </button>

        {error && (
          <p className="mt-3 text-sm text-red-400 bg-red-950/40 border border-red-800 rounded px-3 py-2">
            {error}
          </p>
        )}
      </section>

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
          </div>

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
    </main>
  );
}
