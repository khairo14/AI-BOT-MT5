"use client";

import { useEffect, useState, useCallback } from "react";
import {
  fetchAIStatus,
  fetchRLStatus,
  fetchRLHistory,
  fetchRLWinRateByState,
  fetchMemoryStats,
  fetchOptimizerStatus,
  trainSymbol,
  trainAllSymbols,
  runOptimizer,
  runOptimizerAll,
  fetchAppConfig,
  patchAppConfig,
  fetchLstmCalibration,
  fetchLstmAccuracyHistory,
  fetchLstmConfidenceDistribution,
} from "@/lib/api";

//  helpers 
function relTime(iso: string | undefined): string {
  if (!iso) return "—";
  // Normalise malformed ISO strings like "2026-03-20T21:42:17+00:00Z" (extra Z after offset)
  const normalised = iso.replace(/([+-]\d{2}:\d{2})Z$/, "$1");
  const d = new Date(normalised);
  if (isNaN(d.getTime())) return "—";
  const diff = Date.now() - d.getTime();
  const m = Math.floor(diff / 60_000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function pct(v: number | undefined): string {
  if (v == null) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function parseRLState(state: string | undefined): string {
  if (!state) return "—";
  const parts = state.split("_");
  if (parts.length < 5) return state;
  
  const [wr, conf, session, dd, vol] = parts;
  
  const wrMap: Record<string, string> = { low: "↓ Win-rate", med: "→ Win-rate", high: "↑ Win-rate" };
  const confMap: Record<string, string> = { low: "Low Conf", med: "Med Conf", high: "High Conf" };
  const sessionMap: Record<string, string> = { overlap: "Overlap", active: "Active", quiet: "Quiet" };
  const ddMap: Record<string, string> = { low: "Safe", med: "Elevated", high: "Near Limit" };
  const volMap: Record<string, string> = { tight: "Tight", wide: "Wide" };
  
  return `${wrMap[wr] || wr}, ${confMap[conf] || conf}, ${sessionMap[session] || session}, ${ddMap[dd] || dd}, ${volMap[vol] || vol}`;
}

//  types 
type AIStatus = Record<string, { accuracy?: number; bars_used?: number; trained_at?: string; training?: boolean; trained?: boolean; ensemble_size?: number; last_rejection?: { at: string; accuracy: number; threshold: number; gate: string } }>;
type RLStatus = Record<string, { confidence_threshold?: number; risk_factor?: number; q_states?: number; last_state?: string }>;
type RLHistory = {
  snapshots?: Array<{ timestamp: string; conf_thresh: number; risk_factor: number; n_updates: number }>;
  trend?: { conf_thresh_change?: number; risk_factor_change?: number } | null;
};
type RLStateRow = { state: string; win_rate: number; samples: number; avg_profit: number };
type RLWinRateByState = {
  best_states?: RLStateRow[];
  worst_states?: RLStateRow[];
};
type MemStats = { total?: number; wins?: number; win_rate?: number; avg_pnl?: number; tp_hits?: number; sl_hits?: number };
type CalibrationBin = { bin_range: string; predicted_conf: number; actual_win_rate: number; sample_count: number; calibration_error: number };
type CalibrationData = {
  calibration_curve?: CalibrationBin[];
  overall_accuracy?: number;
  mean_calibration_error?: number;
  is_well_calibrated?: boolean;
  total_trades?: number;
};
type AccuracySnapshot = { timestamp: string; overall_accuracy: number; correct: number; total: number };
type AccuracyHistory = {
  snapshots?: AccuracySnapshot[];
  statistics?: { min_accuracy: number; max_accuracy: number; avg_accuracy: number; latest_accuracy: number; snapshots_count?: number };
  degradation_alert?: { is_degraded: boolean; degradation_pct: number; recommendation: string };
};
type ConfBin = { confidence_range: string; bin_start: number; bin_end: number; count: number; percentage: number };
type ConfidenceDist = {
  histogram?: ConfBin[];
  statistics?: { mean: number; median: number; std_dev: number; min: number; max: number };
  balance?: { is_well_distributed: boolean; max_bin_percentage: number };
  total_trades?: number;
};
type OptimizerJob = {  strategy?: string;
  symbol?: string;
  trading_type?: string;
  best_score?: number;
  n_signals?: number;
  last_optimized_at?: string;
  best_params?: Record<string, unknown>;
  bars_used?: number;
  running?: boolean;
  queued?: boolean;
};
type OptimizerStatus = {
  jobs?: Record<string, OptimizerJob>;
  available_strategies?: string[];
};

const MODES = ["scalping", "day_trading", "swing"] as const;

/** Optimizer job key helpers */
const job_strat = (key: string, job: OptimizerJob) => job.strategy ?? key.split("__")[0];
const job_sym   = (key: string, job: OptimizerJob) => job.symbol   ?? key.split("__")[1] ?? key;

/** Safely split a model key like "US100Cash_day_trading" → ["US100Cash", "day_trading"] */
function parseModelKey(key: string): [string, string] {
  for (const tt of ["day_trading", "scalping", "swing"] as const) {
    if (key.endsWith(`_${tt}`)) return [key.slice(0, -(tt.length + 1)), tt];
  }
  const idx = key.lastIndexOf("_");
  return idx >= 0 ? [key.slice(0, idx), key.slice(idx + 1)] : [key, ""];
}

//  component 
export default function MLPage() {
  const [aiStatus, setAiStatus] = useState<AIStatus>({});
  const [rlStatus, setRlStatus] = useState<RLStatus>({});
  const [rlHistory, setRlHistory] = useState<RLHistory>({});
  const [rlWinRate, setRlWinRate] = useState<RLWinRateByState>({});
  const [rlInsightsMode, setRlInsightsMode] = useState<(typeof MODES)[number]>("day_trading");
  const [rlInsightsDays, setRlInsightsDays] = useState(7);
  const [memStats, setMemStats] = useState<Record<string, MemStats>>({});
  const [optStatus, setOptStatus] = useState<OptimizerStatus>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  // Model calibration state
  const [calibration, setCalibration] = useState<CalibrationData>({});
  const [accuracyHistory, setAccuracyHistory] = useState<AccuracyHistory>({});
  const [confidenceDist, setConfidenceDist] = useState<ConfidenceDist>({});

  // AI gate settings
  const [lstmEnabled, setLstmEnabled]   = useState(true);
  const [rlEnabled, setRlEnabled]       = useState(true);
  const [gateEnabled, setGateEnabled]   = useState(false);
  const [gateThreshold, setGateThreshold] = useState(60);
  const [gateSaving, setGateSaving]     = useState(false);
  const [scorerWeights, setScorerWeights] = useState({ lstm: 40, rr: 25, trend: 20, volume: 15 });
  const [scorerSaving, setScorerSaving] = useState(false);
  const [scalScorerWeights, setScalScorerWeights] = useState({ lstm: 15, rr: 25, trend: 40, volume: 20 });
  const [scalScorerSaving, setScalScorerSaving] = useState(false);
  const [swingScorerWeights, setSwingScorerWeights] = useState({ lstm: 40, rr: 30, trend: 25, volume: 5 });
  const [swingScorerSaving, setSwingScorerSaving] = useState(false);

  // LSTM table filter/sort
  const [lstmSearch, setLstmSearch] = useState("");
  type LstmSortKey = "sym" | "accuracy" | "bars_used" | "status";
  const [lstmSort, setLstmSort] = useState<{ key: LstmSortKey; dir: "asc" | "desc" }>({ key: "sym", dir: "asc" });

  // Optimizer table filter/sort
  const [optSearch, setOptSearch] = useState("");
  type OptSortKey = "strategy" | "symbol" | "score" | "signals" | "last_run";
  const [optSort, setOptSort] = useState<{ key: OptSortKey; dir: "asc" | "desc" }>({ key: "symbol", dir: "asc" });

  const load = useCallback(async () => {
    try {
      const [ai, rl, rlHist, rlStates, opt, appCfg, cal, accHist, confDist, ...mems] = await Promise.allSettled([
        fetchAIStatus(),
        fetchRLStatus(),
        fetchRLHistory(rlInsightsMode, rlInsightsDays),
        fetchRLWinRateByState(rlInsightsMode, 1),
        fetchOptimizerStatus(),
        fetchAppConfig(),
        fetchLstmCalibration(5),
        fetchLstmAccuracyHistory(undefined, 30),
        fetchLstmConfidenceDistribution(),
        ...MODES.map((m) => fetchMemoryStats(m)),
      ]);
      if (ai.status === "fulfilled") setAiStatus(ai.value ?? {});
      if (rl.status === "fulfilled") setRlStatus(rl.value ?? {});
      if (rlHist.status === "fulfilled") setRlHistory((rlHist.value as RLHistory) ?? {});
      if (rlStates.status === "fulfilled") setRlWinRate((rlStates.value as RLWinRateByState) ?? {});
      if (opt.status === "fulfilled") setOptStatus(opt.value ?? {});
      if (cal.status === "fulfilled") setCalibration((cal.value as CalibrationData) ?? {});
      if (accHist.status === "fulfilled") setAccuracyHistory((accHist.value as AccuracyHistory) ?? {});
      if (confDist.status === "fulfilled") setConfidenceDist((confDist.value as ConfidenceDist) ?? {});
      if (appCfg.status === "fulfilled") {
        const ai_cfg = appCfg.value?.ai;
        if (ai_cfg) {
          setGateEnabled(Boolean(ai_cfg.confidence_filter_enabled));
          setGateThreshold(Number(ai_cfg.confidence_threshold ?? 60));
          setLstmEnabled(Boolean(ai_cfg.price_prediction_enabled ?? true));
          setRlEnabled(Boolean(ai_cfg.rl_agent_enabled ?? true));
          const w = ai_cfg.scorer_weights;
          if (w) {
            setScorerWeights({
              lstm:   Math.round((Number(w.lstm)   || 0.40) * 100),
              rr:     Math.round((Number(w.rr)     || 0.25) * 100),
              trend:  Math.round((Number(w.trend)  || 0.20) * 100),
              volume: Math.round((Number(w.volume) || 0.15) * 100),
            });
          }
          const sw = ai_cfg.scalping_scorer_weights;
          if (sw) {
            setScalScorerWeights({
              lstm:   Math.round((Number(sw.lstm)   || 0.15) * 100),
              rr:     Math.round((Number(sw.rr)     || 0.25) * 100),
              trend:  Math.round((Number(sw.trend)  || 0.40) * 100),
              volume: Math.round((Number(sw.volume) || 0.20) * 100),
            });
          }
          const sw2 = ai_cfg.swing_scorer_weights;
          if (sw2) {
            setSwingScorerWeights({
              lstm:   Math.round((Number(sw2.lstm)   || 0.40) * 100),
              rr:     Math.round((Number(sw2.rr)     || 0.30) * 100),
              trend:  Math.round((Number(sw2.trend)  || 0.25) * 100),
              volume: Math.round((Number(sw2.volume) || 0.05) * 100),
            });
          }
        }
      }
      const statsMap: Record<string, MemStats> = {};
      MODES.forEach((m, i) => {
        const r = mems[i];
        if (r.status === "fulfilled") statsMap[m] = r.value ?? {};
      });
      setMemStats(statsMap);
    } catch (_) {/* silently skip on network error */}
  }, [rlInsightsMode, rlInsightsDays]);

  useEffect(() => {
    load();
    const id = setInterval(load, 30_000);
    return () => clearInterval(id);
  }, [load]);

  async function doAction(key: string, fn: () => Promise<unknown>) {
    setBusy((b) => ({ ...b, [key]: true }));
    try { await fn(); } catch (_) {/* ignore */}
    setBusy((b) => ({ ...b, [key]: false }));
    await load();
  }

  //  sub-components (inlined for brevity) 

  // Section header
  const SectionHeader = ({ title, sub }: { title: string; sub: string }) => (
    <div className="mb-4">
      <h2 className="text-lg font-semibold text-white">{title}</h2>
      <p className="text-xs text-gray-500 mt-0.5">{sub}</p>
    </div>
  );

  // Badge
  const Badge = ({ ok, label }: { ok: boolean; label: string }) => (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${ok ? "bg-emerald-900 text-emerald-300" : "bg-amber-900 text-amber-300"}`}>
      {label}
    </span>
  );

  // Sortable column header
  function SortTh<K extends string>({
    label, sortKey, current, onSort, className = "",
  }: { label: string; sortKey: K; current: { key: K; dir: "asc" | "desc" }; onSort: (k: K) => void; className?: string }) {
    const active = current.key === sortKey;
    return (
      <th
        className={`pb-2 pt-2 pr-4 font-medium cursor-pointer select-none hover:text-gray-300 ${className}`}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span className="ml-1 text-gray-600">{active ? (current.dir === "asc" ? "→" : "↓") : "↕"}</span>
      </th>
    );
  }

  //  render 
  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-8 w-full">
      <div>
        <h1 className="text-2xl font-bold text-white">AI / ML Brain</h1>
        <p className="text-sm text-gray-500 mt-1">
          Live model status, optimizer results, and trade memory — auto-refreshes every 30 s.
        </p>
      </div>

      {/*  1. LSTM Models  */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <SectionHeader title="LSTM Prediction Models" sub="One model per symbol ├ù trading mode, trained on OHLCV + indicators." />
          <button
            disabled={busy["train_all"]}
            onClick={() => doAction("train_all", () => trainAllSymbols())}
            className="px-3 py-1.5 text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-50 rounded font-medium"
          >
            {busy["train_all"] ? "Queuing..." : "Retrain All"}
          </button>
        </div>

        {Object.keys(aiStatus).length === 0 ? (
          <p className="text-sm text-gray-600">No models loaded (bot may be offline).</p>
        ) : (() => {
          // filter
          const q = lstmSearch.trim().toLowerCase();
          const filtered = Object.entries(aiStatus).filter(([key]) => {
            const [sym, type] = parseModelKey(key);
            return !q || sym.toLowerCase().includes(q) || type.toLowerCase().includes(q);
          });
          // sort
          const sorted = [...filtered].sort(([ka, ma], [kb, mb]) => {
            const [sa, ta] = parseModelKey(ka);
            const [sb, tb] = parseModelKey(kb);
            let cmp = 0;
            if (lstmSort.key === "sym")       cmp = sa.localeCompare(sb) || ta.localeCompare(tb);
            else if (lstmSort.key === "accuracy")  cmp = (ma.accuracy ?? -1) - (mb.accuracy ?? -1);
            else if (lstmSort.key === "bars_used") cmp = (ma.bars_used ?? 0) - (mb.bars_used ?? 0);
            else if (lstmSort.key === "status")    cmp = Number(ma.training) - Number(mb.training);
            return lstmSort.dir === "asc" ? cmp : -cmp;
          });
          const toggleLstmSort = (k: LstmSortKey) =>
            setLstmSort((s) => ({ key: k, dir: s.key === k && s.dir === "asc" ? "desc" : "asc" }));
          return (
            <>
              <div className="mb-2">
                <input
                  type="text"
                  placeholder="Search symbol or mode..."
                  value={lstmSearch}
                  onChange={(e) => setLstmSearch(e.target.value)}
                  className="w-64 px-3 py-1.5 text-sm bg-gray-900 border border-gray-700 rounded text-gray-200 placeholder-gray-600 focus:outline-none focus:border-blue-600"
                />
                <span className="ml-3 text-xs text-gray-600">{sorted.length} / {Object.keys(aiStatus).length} models</span>
              </div>
              <div className="overflow-x-auto max-h-96 overflow-y-auto border border-gray-800 rounded-lg">
                <table className="w-full text-sm border-collapse">
                  <thead className="sticky top-0 bg-gray-950 z-10">
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <SortTh label="Symbol / Mode" sortKey="sym"       current={lstmSort} onSort={toggleLstmSort} className="pl-2" />
                      <SortTh label="Accuracy"      sortKey="accuracy"  current={lstmSort} onSort={toggleLstmSort} />
                      <SortTh label="Bars Used"     sortKey="bars_used" current={lstmSort} onSort={toggleLstmSort} />
                      <th className="pb-2 pt-2 pr-4 font-medium">Last Trained</th>
                      <SortTh label="Status"        sortKey="status"    current={lstmSort} onSort={toggleLstmSort} />
                      <th className="pb-2 pt-2 font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map(([key, m]) => {
                      const [sym, type] = parseModelKey(key);
                      const bKey = `train_${key}`;
                      return (
                        <tr key={key} className="border-b border-gray-800/50 hover:bg-gray-900/40">
                          <td className="py-2 pl-2 pr-4">
                            <span className="font-semibold text-white">{sym}</span>
                            <span className="ml-2 text-xs text-gray-500">{type.replace("_", " ")}</span>
                          </td>
                          <td className="py-2 pr-4">
                            {m.accuracy != null ? pct(m.accuracy) : "—"}
                            {m.ensemble_size != null && m.ensemble_size > 1 && (
                              <span className="ml-1 text-xs text-blue-400" title={`Ensemble (${m.ensemble_size} members)`}>×{m.ensemble_size}</span>
                            )}
                          </td>
                          <td className="py-2 pr-4">{m.bars_used ?? "—"}</td>
                          <td className="py-2 pr-4 text-gray-400">{relTime(m.trained_at)}</td>
                          <td className="py-2 pr-4">
                            {m.training ? (
                              <Badge ok={false} label="Training" />
                            ) : m.last_rejection && !m.trained ? (
                              <span
                                className="text-xs px-1.5 py-0.5 rounded bg-amber-900/60 text-amber-400 cursor-help"
                                title={`Last retrain rejected — accuracy ${(m.last_rejection.accuracy * 100).toFixed(1)}% below ${(m.last_rejection.threshold * 100).toFixed(0)}% ${m.last_rejection.gate} gate. Old model still active.`}
                              >
                                Rejected {(m.last_rejection.accuracy * 100).toFixed(1)}%
                              </span>
                            ) : (
                              <Badge ok={true} label="Ready" />
                            )}
                          </td>
                          <td className="py-2">
                            <button
                              disabled={busy[bKey] || m.training}
                              onClick={() => doAction(bKey, () => trainSymbol(sym, type || "scalping"))}
                              className="px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded"
                            >
                              {busy[bKey] ? "..." : "Retrain"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          );
        })()}
      </section>

      {/*  2. RL Agents  */}
      <section>
        <SectionHeader title="RL Agents" sub="Tabular Q-learning agent per trading mode — adjusts confidence threshold and risk factor over time." />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {MODES.map((mode) => {
            const agent = rlStatus[mode] ?? {};
            return (
              <div key={mode} className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
                <div className="flex items-center justify-between">
                  <p className="font-semibold text-white capitalize">{mode.replace("_", " ")}</p>
                </div>
                <div className="text-sm space-y-1 text-gray-400">
                  <div className="flex justify-between">
                    <span>Confidence threshold</span>
                    <span className="text-white font-medium">{pct(agent.confidence_threshold)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Risk factor</span>
                    <span className="text-white font-medium">{agent.risk_factor != null ? agent.risk_factor.toFixed(2) : "—"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Q-table states</span>
                    <span className="text-white font-medium">{agent.q_states ?? "—"}</span>
                  </div>
                  <div className="flex flex-col">
                    <span className="text-gray-400 text-sm inline-flex items-center gap-1">
                      Last state
                      <span 
                        className="cursor-help text-blue-400" 
                        title="Win-rate: ↓ Low (<40%), → Med (40-60%), ↑ High (>60%)&#10;Confidence: Low/Med/High - avg signal confidence&#10;Session: Overlap (peak 12-17 UTC), Active (London/NY), Quiet (Asian)&#10;Drawdown: Safe (<1.5%), Elevated (1.5-3%), Near Limit (≥3%)&#10;Volatility: Tight (<1% SL), Wide (≥1% - crypto/gold/oil)"
                      >
                        ⓘ
                      </span>
                    </span>
                    <span className="text-white font-medium text-xs">{parseRLState(agent.last_state)}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <div className="mt-4 bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-4">
          <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-3">
            <div>
              <p className="text-sm font-medium text-white">RL Insights</p>
              <p className="text-xs text-gray-500">Shows adaptation trend and which market states are performing best vs worst.</p>
            </div>
            <div className="flex items-center gap-2">
              <label className="text-xs text-gray-500">Mode</label>
              <select
                value={rlInsightsMode}
                onChange={(e) => setRlInsightsMode(e.target.value as (typeof MODES)[number])}
                className="px-2 py-1 text-xs bg-gray-950 border border-gray-700 rounded text-gray-200"
              >
                {MODES.map((m) => (
                  <option key={m} value={m}>{m.replace("_", " ")}</option>
                ))}
              </select>
              <label className="text-xs text-gray-500">Days</label>
              <select
                value={rlInsightsDays}
                onChange={(e) => setRlInsightsDays(Number(e.target.value))}
                className="px-2 py-1 text-xs bg-gray-950 border border-gray-700 rounded text-gray-200"
              >
                {[7, 14, 30].map((d) => <option key={d} value={d}>{d}d</option>)}
              </select>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <div className="bg-gray-950 border border-gray-800 rounded p-2">
              <p className="text-gray-500">Snapshots</p>
              <p className="text-white font-semibold">{rlHistory.snapshots?.length ?? 0}</p>
            </div>
            <div className="bg-gray-950 border border-gray-800 rounded p-2">
              <p className="text-gray-500">Current Threshold</p>
              <p className="text-white font-semibold">
                {rlHistory.snapshots?.length ? pct(rlHistory.snapshots[rlHistory.snapshots.length - 1].conf_thresh) : "—"}
              </p>
            </div>
            <div className="bg-gray-950 border border-gray-800 rounded p-2">
              <p className="text-gray-500">Current Risk</p>
              <p className="text-white font-semibold">
                {rlHistory.snapshots?.length ? rlHistory.snapshots[rlHistory.snapshots.length - 1].risk_factor.toFixed(2) : "—"}
              </p>
            </div>
            <div className="bg-gray-950 border border-gray-800 rounded p-2">
              <p className="text-gray-500">Trend</p>
              <p className="text-white font-semibold">
                {rlHistory.trend == null
                  ? "—"
                  : <>C {((rlHistory.trend.conf_thresh_change ?? 0) >= 0 ? "+" : "") + (rlHistory.trend.conf_thresh_change ?? 0).toFixed(3)}{" | "}R {((rlHistory.trend.risk_factor_change ?? 0) >= 0 ? "+" : "") + (rlHistory.trend.risk_factor_change ?? 0).toFixed(3)}</>
                }
              </p>
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-emerald-300 mb-2">Best Market Conditions</p>
              <div className="overflow-auto max-h-52">
                <table className="w-full text-xs border-collapse">
                  <thead className="sticky top-0 bg-gray-950 z-10">
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <th className="py-2 px-2 font-medium">
                        <span className="inline-flex items-center gap-1">
                          State
                          <span 
                            className="cursor-help text-blue-400" 
                            title="Win-rate: ↓ Low (<40%), → Med (40-60%), ↑ High (>60%)&#10;Confidence: Low/Med/High - avg signal confidence&#10;Session: Overlap (peak 12-17 UTC), Active (London/NY), Quiet (Asian)&#10;Drawdown: Safe (<1.5%), Elevated (1.5-3%), Near Limit (≥3%)&#10;Volatility: Tight (<1% SL), Wide (≥1% - crypto/gold/oil)"
                          >
                            ⓘ
                          </span>
                        </span>
                      </th>
                      <th className="py-2 px-2 font-medium text-right">Win Rate</th>
                      <th className="py-2 px-2 font-medium text-right">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(rlWinRate.best_states ?? []).map((s) => (
                      <tr key={`best_${s.state}`} className="border-b border-gray-800/40">
                        <td className="py-1.5 px-2 text-gray-200">{parseRLState(s.state)}</td>
                        <td className="py-1.5 px-2 text-right text-emerald-300">{(s.win_rate * 100).toFixed(1)}%</td>
                        <td className="py-1.5 px-2 text-right text-white">{s.samples}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="border border-gray-800 rounded-lg p-3">
              <p className="text-xs text-rose-300 mb-2">Weak Market Conditions</p>
              <div className="overflow-auto max-h-52">
                <table className="w-full text-xs border-collapse">
                  <thead className="sticky top-0 bg-gray-950 z-10">
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <th className="py-2 px-2 font-medium">
                        <span className="inline-flex items-center gap-1">
                          State
                          <span 
                            className="cursor-help text-blue-400" 
                            title="Win-rate: ↓ Low (<40%), → Med (40-60%), ↑ High (>60%)&#10;Confidence: Low/Med/High - avg signal confidence&#10;Session: Overlap (peak 12-17 UTC), Active (London/NY), Quiet (Asian)&#10;Drawdown: Safe (<1.5%), Elevated (1.5-3%), Near Limit (≥3%)&#10;Volatility: Tight (<1% SL), Wide (≥1% - crypto/gold/oil)"
                          >
                            ⓘ
                          </span>
                        </span>
                      </th>
                      <th className="py-2 px-2 font-medium text-right">Win Rate</th>
                      <th className="py-2 px-2 font-medium text-right">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(rlWinRate.worst_states ?? []).map((s) => (
                      <tr key={`worst_${s.state}`} className="border-b border-gray-800/40">
                        <td className="py-1.5 px-2 text-gray-200">{parseRLState(s.state)}</td>
                        <td className="py-1.5 px-2 text-right text-rose-300">{(s.win_rate * 100).toFixed(1)}%</td>
                        <td className="py-1.5 px-2 text-right text-white">{s.samples}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/*  2b. Model Calibration  */}
      <section>
        <SectionHeader
          title="Model Calibration &amp; Performance"
          sub="LSTM prediction quality — compares predicted confidence against actual win rate, tracks accuracy over time, and shows confidence distribution."
        />
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">

          {/* Calibration Curve */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-sm font-medium text-white">Calibration Curve</p>
              <span className={`text-xs px-2 py-0.5 rounded font-medium ${calibration.is_well_calibrated ? "bg-emerald-900 text-emerald-300" : "bg-amber-900 text-amber-300"}`}>
                {calibration.is_well_calibrated ? "Well calibrated" : "Needs calibration"}
              </span>
            </div>
            <div className="text-xs text-gray-500 mb-3 flex gap-4">
              <span>Overall acc: <span className="text-white font-medium">{calibration.overall_accuracy != null ? `${(calibration.overall_accuracy * 100).toFixed(1)}%` : "—"}</span></span>
              <span>Mean error: <span className={`font-medium ${(calibration.mean_calibration_error ?? 1) < 0.15 ? "text-emerald-400" : "text-amber-400"}`}>{calibration.mean_calibration_error != null ? `${(calibration.mean_calibration_error * 100).toFixed(1)}%` : "—"}</span></span>
              <span>Trades: <span className="text-white font-medium">{calibration.total_trades ?? "—"}</span></span>
            </div>
            {(calibration.calibration_curve ?? []).length === 0 ? (
              <p className="text-xs text-gray-600">No calibration data yet.</p>
            ) : (
              <div className="space-y-1.5">
                {/* Perfect calibration reference line note */}
                <p className="text-xs text-gray-600 mb-2">Predicted confidence vs actual win rate (ideal = equal)</p>
                {(calibration.calibration_curve ?? []).map((bin) => {
                  const predicted = bin.predicted_conf * 100;
                  const actual = bin.actual_win_rate * 100;
                  const gap = actual - predicted;
                  const barWidth = Math.min(Math.max(actual, 0), 100);
                  const isOverconfident = gap < -5;
                  const isUnderconfident = gap > 5;
                  return (
                    <div key={bin.bin_range} className="text-xs">
                      <div className="flex justify-between text-gray-400 mb-0.5">
                        <span>{bin.bin_range}</span>
                        <span className="flex gap-3">
                          <span className="text-gray-500">pred {predicted.toFixed(0)}%</span>
                          <span className={isOverconfident ? "text-rose-400" : isUnderconfident ? "text-emerald-400" : "text-gray-300"}>
                            actual {actual.toFixed(0)}%
                          </span>
                          <span className="text-gray-600">n={bin.sample_count}</span>
                        </span>
                      </div>
                      <div className="flex gap-1 h-2">
                        {/* Predicted bar (gray) */}
                        <div className="flex-1 bg-gray-800 rounded-sm overflow-hidden relative">
                          <div
                            className="h-full bg-gray-600 rounded-sm"
                            style={{ width: `${Math.min(predicted, 100)}%` }}
                          />
                        </div>
                        {/* Actual bar (colored by calibration quality) */}
                        <div className="flex-1 bg-gray-800 rounded-sm overflow-hidden">
                          <div
                            className={`h-full rounded-sm ${isOverconfident ? "bg-rose-600" : isUnderconfident ? "bg-emerald-600" : "bg-blue-600"}`}
                            style={{ width: `${barWidth}%` }}
                          />
                        </div>
                      </div>
                    </div>
                  );
                })}
                <div className="flex gap-4 mt-2 text-xs text-gray-600">
                  <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 bg-gray-600 rounded-sm"></span>Predicted</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 bg-blue-600 rounded-sm"></span>Actual (calibrated)</span>
                  <span className="flex items-center gap-1"><span className="inline-block w-2 h-2 bg-rose-600 rounded-sm"></span>Overconfident</span>
                </div>
              </div>
            )}
          </div>

          {/* Accuracy History */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-sm font-medium text-white">Accuracy Over Time</p>
              {accuracyHistory.degradation_alert?.is_degraded && (
                <span className="text-xs px-2 py-0.5 rounded font-medium bg-rose-900 text-rose-300">Degraded</span>
              )}
            </div>
            {accuracyHistory.statistics && (
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs mb-3">
                <div className="text-gray-500">Latest</div>
                <div className="text-white font-medium text-right">{((accuracyHistory.statistics.latest_accuracy ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Average</div>
                <div className="text-white text-right">{((accuracyHistory.statistics.avg_accuracy ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Best</div>
                <div className="text-emerald-400 text-right">{((accuracyHistory.statistics.max_accuracy ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Worst</div>
                <div className="text-rose-400 text-right">{((accuracyHistory.statistics.min_accuracy ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Snapshots</div>
                <div className="text-white text-right">{accuracyHistory.statistics.snapshots_count}</div>
              </div>
            )}
            {(accuracyHistory.snapshots ?? []).length === 0 ? (
              <p className="text-xs text-gray-600">No accuracy history yet.</p>
            ) : (
              <div className="space-y-1">
                <p className="text-xs text-gray-600 mb-2">Last 30 days — each bar is one snapshot</p>
                {[...(accuracyHistory.snapshots ?? [])].reverse().slice(0, 10).map((snap, idx) => {
                  const accPct = snap.overall_accuracy * 100;
                  const d = new Date(snap.timestamp.replace(/([+-]\d{2}:\d{2})Z$/, "$1"));
                  const label = isNaN(d.getTime()) ? snap.timestamp : d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
                  return (
                    <div key={idx} className="text-xs">
                      <div className="flex justify-between text-gray-400 mb-0.5">
                        <span>{label}</span>
                        <span className={accPct >= 50 ? "text-emerald-400" : accPct >= 40 ? "text-amber-400" : "text-rose-400"}>
                          {accPct.toFixed(1)}%
                        </span>
                      </div>
                      <div className="h-1.5 bg-gray-800 rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full ${accPct >= 50 ? "bg-emerald-600" : accPct >= 40 ? "bg-amber-600" : "bg-rose-600"}`}
                          style={{ width: `${accPct}%` }}
                        />
                      </div>
                    </div>
                  );
                })}
                {accuracyHistory.degradation_alert && (
                  <p className={`text-xs mt-2 ${accuracyHistory.degradation_alert.is_degraded ? "text-rose-400" : "text-gray-600"}`}>
                    {accuracyHistory.degradation_alert.recommendation}
                  </p>
                )}
              </div>
            )}
          </div>

          {/* Confidence Distribution */}
          <div className="bg-gray-900 border border-gray-800 rounded-lg p-4">
            <div className="flex items-center justify-between mb-3">
              <p className="text-sm font-medium text-white">Confidence Distribution</p>
              {confidenceDist.balance && (
                <span className={`text-xs px-2 py-0.5 rounded font-medium ${confidenceDist.balance.is_well_distributed ? "bg-emerald-900 text-emerald-300" : "bg-amber-900 text-amber-300"}`}>
                  {confidenceDist.balance.is_well_distributed ? "Well spread" : "Concentrated"}
                </span>
              )}
            </div>
            {confidenceDist.statistics && (
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs mb-3">
                <div className="text-gray-500">Mean conf</div>
                <div className="text-white font-medium text-right">{((confidenceDist.statistics.mean ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Median</div>
                <div className="text-white text-right">{((confidenceDist.statistics.median ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Std dev</div>
                <div className="text-white text-right">{((confidenceDist.statistics.std_dev ?? 0) * 100).toFixed(1)}%</div>
                <div className="text-gray-500">Trades</div>
                <div className="text-white text-right">{confidenceDist.total_trades ?? "—"}</div>
              </div>
            )}
            {(confidenceDist.histogram ?? []).length === 0 ? (
              <p className="text-xs text-gray-600">No distribution data yet.</p>
            ) : (() => {
              const maxPct = Math.max(...(confidenceDist.histogram ?? []).map((b) => b.percentage), 1);
              return (
                <div className="space-y-1.5">
                  {(confidenceDist.histogram ?? []).map((bin) => (
                    <div key={bin.confidence_range} className="text-xs">
                      <div className="flex justify-between text-gray-400 mb-0.5">
                        <span>{bin.confidence_range}</span>
                        <span className="flex gap-2">
                          <span className="text-gray-500">{bin.count} trades</span>
                          <span className="text-white font-medium">{bin.percentage.toFixed(1)}%</span>
                        </span>
                      </div>
                      <div className="h-2 bg-gray-800 rounded-sm overflow-hidden">
                        <div
                          className="h-full bg-indigo-600 rounded-sm"
                          style={{ width: `${(bin.percentage / maxPct) * 100}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              );
            })()}
          </div>

        </div>
      </section>

      {/*  3. Parameter Optimizer  */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <SectionHeader title="Parameter Optimizer" sub="Walk-forward backtest grid search — runs automatically when win-rate drops below 45 % (24 h cooldown)." />
          <button
            disabled={busy["opt_all"]}
            onClick={() => doAction("opt_all", () => runOptimizerAll())}
            className="px-3 py-1.5 text-xs bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded font-medium"
          >
            {busy["opt_all"] ? "Queuing..." : "Optimize All"}
          </button>
        </div>

        {(!optStatus.jobs || Object.keys(optStatus.jobs).length === 0) ? (
          <p className="text-sm text-gray-600">No optimization runs recorded yet.</p>
        ) : (() => {
          const q = optSearch.trim().toLowerCase();
          const allJobs = Object.entries(optStatus.jobs ?? {});
          const filtered = allJobs.filter(([key, job]) => {
            const strat = (job.strategy ?? key.split("__")[0]).toLowerCase();
            const sym   = (job.symbol   ?? key.split("__")[1] ?? key).toLowerCase();
            return !q || strat.includes(q) || sym.includes(q);
          });
          const sorted = [...filtered].sort(([ka, ja], [kb, jb]) => {
            const sa = job_strat(ka, ja), sb = job_strat(kb, jb);
            const sya = job_sym(ka, ja),   syb = job_sym(kb, jb);
            let cmp = 0;
            if (optSort.key === "strategy") cmp = sa.localeCompare(sb);
            else if (optSort.key === "symbol")   cmp = sya.localeCompare(syb);
            else if (optSort.key === "score")    cmp = (ja.best_score ?? -Infinity) - (jb.best_score ?? -Infinity);
            else if (optSort.key === "signals")  cmp = (ja.n_signals ?? 0) - (jb.n_signals ?? 0);
            else if (optSort.key === "last_run") cmp = (ja.last_optimized_at ?? "").localeCompare(jb.last_optimized_at ?? "");
            return optSort.dir === "asc" ? cmp : -cmp;
          });
          const toggleOptSort = (k: OptSortKey) =>
            setOptSort((s) => ({ key: k, dir: s.key === k && s.dir === "asc" ? "desc" : "asc" }));
          return (
            <>
              <div className="mb-2">
                <input
                  type="text"
                  placeholder="Search strategy or symbol..."
                  value={optSearch}
                  onChange={(e) => setOptSearch(e.target.value)}
                  className="w-64 px-3 py-1.5 text-sm bg-gray-900 border border-gray-700 rounded text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-600"
                />
                <span className="ml-3 text-xs text-gray-600">{filtered.length} / {allJobs.length} jobs</span>
              </div>
              <div className="overflow-x-auto max-h-96 overflow-y-auto border border-gray-800 rounded-lg">
                <table className="w-full text-sm border-collapse">
                  <thead className="sticky top-0 bg-gray-950 z-10">
                    <tr className="text-left text-gray-500 border-b border-gray-800">
                      <SortTh label="Strategy" sortKey="strategy" current={optSort} onSort={toggleOptSort} className="pl-2" />
                      <SortTh label="Symbol"   sortKey="symbol"   current={optSort} onSort={toggleOptSort} />
                      <SortTh label="Score"    sortKey="score"    current={optSort} onSort={toggleOptSort} />
                      <SortTh label="Signals"  sortKey="signals"  current={optSort} onSort={toggleOptSort} />
                      <th className="pb-2 pt-2 pr-4 font-medium">Bars</th>
                      <SortTh label="Last Run" sortKey="last_run" current={optSort} onSort={toggleOptSort} />
                      <th className="pb-2 pt-2 pr-4 font-medium">Best Params</th>
                      <th className="pb-2 pt-2 font-medium">Action</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sorted.map(([key, job]) => {
                      const bKey = `opt_${key}`;
                      const strategyName = job_strat(key, job);
                      const symbolName   = job_sym(key, job);
                      return (
                        <tr key={bKey} className="border-b border-gray-800/50 hover:bg-gray-900/40">
                          <td className="py-2 pr-4 pl-2 font-medium text-white">{strategyName}</td>
                          <td className="py-2 pr-4">{symbolName}</td>
                          <td className="py-2 pr-4">
                            {job.running ? (
                              <span className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-amber-900 text-amber-300 animate-pulse">Running...</span>
                            ) : job.queued ? (
                              <span className="inline-block px-2 py-0.5 rounded text-xs font-medium bg-blue-900 text-blue-300">Queued</span>
                            ) : (
                              job.best_score != null ? job.best_score.toFixed(3) : "—"
                            )}
                          </td>
                          <td className="py-2 pr-4">{job.n_signals ?? "—"}</td>
                          <td className="py-2 pr-4 text-gray-500">{job.bars_used ?? "—"}</td>
                          <td className="py-2 pr-4 text-gray-400">{relTime(job.last_optimized_at)}</td>
                          <td className="py-2 pr-4 text-gray-400 text-xs max-w-48 truncate">
                            {job.best_params ? JSON.stringify(job.best_params) : "—"}
                          </td>
                          <td className="py-2">
                            <button
                              disabled={busy[bKey]}
                              onClick={() => doAction(bKey, () => runOptimizer(strategyName, symbolName, job.trading_type ?? "day_trading"))}
                              className="px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded"
                            >
                              {busy[bKey] ? "..." : "Run"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          );
        })()}
      </section>

      {/*  4. Trade Memory  */}
      <section>
        <SectionHeader title="Trade Memory" sub="Outcome log used for LSTM retraining and optimizer triggers." />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {MODES.map((mode) => {
            const s = memStats[mode] ?? {};
            return (
              <div key={mode} className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
                <p className="font-semibold text-white capitalize">{mode.replace("_", " ")}</p>
                <div className="text-sm space-y-1 text-gray-400">
                  <div className="flex justify-between">
                    <span>Total trades</span>
                    <span className="text-white font-medium">{s.total ?? "—"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>Win rate</span>
                    <span className={`font-medium ${(s.win_rate ?? 0) >= 0.5 ? "text-emerald-400" : "text-red-400"}`}>
                      {pct(s.win_rate)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>Avg PnL</span>
                    <span className={`font-medium ${(s.avg_pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                      {s.avg_pnl != null ? s.avg_pnl.toFixed(2) : "—"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span>TP hits</span>
                    <span className="text-white font-medium">{s.tp_hits ?? "—"}</span>
                  </div>
                  <div className="flex justify-between">
                    <span>SL hits</span>
                    <span className="text-white font-medium">{s.sl_hits ?? "—"}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/*  0. AI Controls  */}
      <section>
        <SectionHeader
          title="AI Controls"
          sub="Master switches for each AI component. Changes take effect immediately — no restart needed."
        />
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-5">

          {/* LSTM Prediction toggle */}
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-white font-medium">LSTM Price Prediction</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When off, the LSTM sub-score is replaced with 0.5 neutral (R:R, trend &amp; volume still score normally).
              </p>
            </div>
            <button
              onClick={async () => {
                const next = !lstmEnabled;
                setLstmEnabled(next);
                setGateSaving(true);
                try {
                  await patchAppConfig({ ai: { price_prediction_enabled: next } });
                } catch (_) { setLstmEnabled(!next); }
                setGateSaving(false);
              }}
              className={`inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                lstmEnabled ? "bg-blue-600" : "bg-gray-700"
              }`}
            >
              <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform transform ${
                lstmEnabled ? "translate-x-6" : "translate-x-1"
              }`} />
            </button>
          </div>

          {/* RL Agent toggle */}
          <div className="flex items-center justify-between border-t border-gray-800 pt-4">
            <div>
              <p className="text-sm text-white font-medium">RL Agent Gate</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When off, RL confidence gate is bypassed and lot sizing uses the raw risk.json value with no RL multiplier.
              </p>
            </div>
            <button
              onClick={async () => {
                const next = !rlEnabled;
                setRlEnabled(next);
                setGateSaving(true);
                try {
                  await patchAppConfig({ ai: { rl_agent_enabled: next } });
                } catch (_) { setRlEnabled(!next); }
                setGateSaving(false);
              }}
              className={`inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                rlEnabled ? "bg-blue-600" : "bg-gray-700"
              }`}
            >
              <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform transform ${
                rlEnabled ? "translate-x-6" : "translate-x-1"
              }`} />
            </button>
          </div>

          {/* Static gate toggle */}
          <div className="flex items-center justify-between border-t border-gray-800 pt-4">
            <div>
              <p className="text-sm text-white font-medium">Static Confidence Filter</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When on, signals with confidence below the threshold are hard-blocked before the RL gate.
              </p>
            </div>
            <button
              onClick={async () => {
                const next = !gateEnabled;
                setGateEnabled(next);
                setGateSaving(true);
                try {
                  await patchAppConfig({ ai: { confidence_filter_enabled: next } });
                } catch (_) { setGateEnabled(!next); }
                setGateSaving(false);
              }}
              className={`inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                gateEnabled ? "bg-blue-600" : "bg-gray-700"
              }`}
            >
              <span className={`inline-block h-4 w-4 rounded-full bg-white transition-transform transform ${
                gateEnabled ? "translate-x-6" : "translate-x-1"
              }`} />
            </button>
          </div>

          {/* Threshold slider — only shown when static gate is on */}
          {gateEnabled && (
            <div className="space-y-2 pl-4 border-l-2 border-blue-800">
              <div className="flex items-center justify-between">
                <p className="text-sm text-white font-medium">Minimum confidence threshold</p>
                <span className="text-blue-400 font-semibold text-sm">{gateThreshold}%</span>
              </div>
              <input
                type="range"
                min={40}
                max={85}
                step={1}
                value={gateThreshold}
                onChange={(e) => setGateThreshold(Number(e.target.value))}
                onMouseUp={async () => {
                  setGateSaving(true);
                  try {
                    await patchAppConfig({ ai: { confidence_threshold: gateThreshold } });
                  } catch (_) {}
                  setGateSaving(false);
                }}
                className="w-full accent-blue-500"
              />
              <div className="flex justify-between text-xs text-gray-600">
                <span>40% — permissive</span>
                <span>60% — balanced</span>
                <span>85% — strict</span>
              </div>
            </div>
          )}

          {gateSaving && <p className="text-xs text-gray-500">Saving...</p>}

          {/* General scorer weights */}
          <div className="border-t border-gray-800 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm text-white font-medium">Signal Scorer Weights <span className="text-xs text-gray-500 ml-1">(Day Trading)</span></p>
              <p className="text-xs text-gray-500">Auto-normalised — any ratio works.</p>
            </div>
            {(["lstm", "rr", "trend", "volume"] as const).map((k) => {
              const labels: Record<string, string> = {
                lstm: "LSTM Prediction",
                rr: "Risk:Reward Quality",
                trend: "Trend Alignment",
                volume: "Volume Confirmation",
              };
              return (
                <div key={k} className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-400">
                    <span>{labels[k]}</span>
                    <span className="text-white font-medium">{scorerWeights[k]}%</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={60}
                    step={1}
                    value={scorerWeights[k]}
                    onChange={(e) => setScorerWeights((prev) => ({ ...prev, [k]: Number(e.target.value) }))}
                    onMouseUp={async () => {
                      setScorerSaving(true);
                      try {
                        await patchAppConfig({
                          ai: {
                            scorer_weights: {
                              lstm:   scorerWeights.lstm   / 100,
                              rr:     scorerWeights.rr     / 100,
                              trend:  scorerWeights.trend  / 100,
                              volume: scorerWeights.volume / 100,
                            },
                          },
                        });
                      } catch (_) {}
                      setScorerSaving(false);
                    }}
                    className="w-full accent-purple-500"
                  />
                </div>
              );
            })}
            {scorerSaving && <p className="text-xs text-gray-500">Saving weights...</p>}
          </div>

          {/* Swing-specific scorer weights */}
          <div className="border-t border-gray-800 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm text-white font-medium">Signal Scorer Weights <span className="text-xs text-teal-400 ml-1">(Swing)</span></p>
              <p className="text-xs text-gray-500">H4 — RR weighted higher, volume less meaningful.</p>
            </div>
            {(["lstm", "rr", "trend", "volume"] as const).map((k) => {
              const labels: Record<string, string> = {
                lstm: "LSTM Prediction",
                rr: "Risk:Reward Quality",
                trend: "Trend Alignment",
                volume: "Volume Confirmation",
              };
              return (
                <div key={k} className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-400">
                    <span>{labels[k]}</span>
                    <span className="text-white font-medium">{swingScorerWeights[k]}%</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={60}
                    step={1}
                    value={swingScorerWeights[k]}
                    onChange={(e) => setSwingScorerWeights((prev) => ({ ...prev, [k]: Number(e.target.value) }))}
                    onMouseUp={async () => {
                      setSwingScorerSaving(true);
                      try {
                        await patchAppConfig({
                          ai: {
                            swing_scorer_weights: {
                              lstm:   swingScorerWeights.lstm   / 100,
                              rr:     swingScorerWeights.rr     / 100,
                              trend:  swingScorerWeights.trend  / 100,
                              volume: swingScorerWeights.volume / 100,
                            },
                          },
                        });
                      } catch (_) {}
                      setSwingScorerSaving(false);
                    }}
                    className="w-full accent-teal-500"
                  />
                </div>
              );
            })}
            {swingScorerSaving && <p className="text-xs text-gray-500">Saving swing weights...</p>}
          </div>

          {/* Scalping-specific scorer weights */}
          <div className="border-t border-gray-800 pt-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm text-white font-medium">Signal Scorer Weights <span className="text-xs text-orange-400 ml-1">(Scalping)</span></p>
              <p className="text-xs text-gray-500">Overrides general weights for scalping signals only.</p>
            </div>
            {(["lstm", "rr", "trend", "volume"] as const).map((k) => {
              const labels: Record<string, string> = {
                lstm: "LSTM Prediction",
                rr: "Risk:Reward Quality",
                trend: "Trend Alignment",
                volume: "Volume Confirmation",
              };
              return (
                <div key={k} className="space-y-1">
                  <div className="flex justify-between text-xs text-gray-400">
                    <span>{labels[k]}</span>
                    <span className="text-white font-medium">{scalScorerWeights[k]}%</span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={60}
                    step={1}
                    value={scalScorerWeights[k]}
                    onChange={(e) => setScalScorerWeights((prev) => ({ ...prev, [k]: Number(e.target.value) }))}
                    onMouseUp={async () => {
                      setScalScorerSaving(true);
                      try {
                        await patchAppConfig({
                          ai: {
                            scalping_scorer_weights: {
                              lstm:   scalScorerWeights.lstm   / 100,
                              rr:     scalScorerWeights.rr     / 100,
                              trend:  scalScorerWeights.trend  / 100,
                              volume: scalScorerWeights.volume / 100,
                            },
                          },
                        });
                      } catch (_) {}
                      setScalScorerSaving(false);
                    }}
                    className="w-full accent-orange-500"
                  />
                </div>
              );
            })}
            {scalScorerSaving && <p className="text-xs text-gray-500">Saving scalping weights...</p>}
          </div>
        </div>
      </section>

      {/*  5. Auto-Training Schedule  */}
      <section>
        <SectionHeader title="Auto-Training Schedule" sub="Rules that trigger background retraining without any manual action." />
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-3 text-sm text-gray-400">
          <div className="flex gap-3">
            <span className="text-blue-400 font-semibold w-48 shrink-0">LSTM retrain</span>
            <span>Every 20th closed trade per symbol ├ù mode. Also triggers on: model age &gt; 7 days (if ΓëÑ 10 trades exist) or 8 consecutive losses (regime change indicator).</span>
          </div>
          <div className="flex gap-3">
            <span className="text-purple-400 font-semibold w-48 shrink-0">Param optimizer</span>
            <span>Triggered when live win-rate drops below 45 % with ΓëÑ 30 new trades since last optimization — 24 h cooldown per strategy ├ù symbol. Walk-forward backtest tests up to 64 parameter combos.</span>
          </div>
          <div className="flex gap-3">
            <span className="text-amber-400 font-semibold w-48 shrink-0">RL agent</span>
            <span>Updates after every closed trade — adjusts confidence threshold and risk factor based on Q-value estimates.</span>
          </div>
          <div className="flex gap-3">
            <span className="text-emerald-400 font-semibold w-48 shrink-0">Best-strategy selector</span>
            <span>Per symbol, the strategy with the highest LSTM confidence (above threshold, R:R ΓëÑ 1.5) wins. Runs at signal time — no extra schedule needed.</span>
          </div>
        </div>
      </section>
    </div>
  );
}
