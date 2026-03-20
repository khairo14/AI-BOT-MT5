"use client";

import { useEffect, useState, useCallback } from "react";
import {
  fetchAIStatus,
  fetchRLStatus,
  fetchMemoryStats,
  fetchOptimizerStatus,
  trainSymbol,
  trainAllSymbols,
  runOptimizer,
  runOptimizerAll,
  fetchAppConfig,
  patchAppConfig,
} from "@/lib/api";

// ── helpers ────────────────────────────────────────────────────────────────
function relTime(iso: string | undefined): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
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

// ── types ──────────────────────────────────────────────────────────────────
type AIStatus = Record<string, { accuracy?: number; bars_used?: number; trained_at?: string; training?: boolean }>;
type RLStatus = Record<string, { confidence_threshold?: number; risk_factor?: number; q_states?: number; last_state?: string }>;
type MemStats = { total?: number; wins?: number; win_rate?: number; avg_pnl?: number; tp_hits?: number; sl_hits?: number };
type OptimizerJob = {
  strategy?: string;
  symbol?: string;
  trading_type?: string;
  best_score?: number;
  n_signals?: number;
  last_optimized_at?: string;
  best_params?: Record<string, unknown>;
  bars_used?: number;
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

// ── component ─────────────────────────────────────────────────────────────
export default function MLPage() {
  const [aiStatus, setAiStatus] = useState<AIStatus>({});
  const [rlStatus, setRlStatus] = useState<RLStatus>({});
  const [memStats, setMemStats] = useState<Record<string, MemStats>>({});
  const [optStatus, setOptStatus] = useState<OptimizerStatus>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  // AI gate settings
  const [gateEnabled, setGateEnabled] = useState(false);
  const [gateThreshold, setGateThreshold] = useState(60);
  const [gateSaving, setGateSaving] = useState(false);

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
      const [ai, rl, opt, appCfg, ...mems] = await Promise.allSettled([
        fetchAIStatus(),
        fetchRLStatus(),
        fetchOptimizerStatus(),
        fetchAppConfig(),
        ...MODES.map((m) => fetchMemoryStats(m)),
      ]);
      if (ai.status === "fulfilled") setAiStatus(ai.value ?? {});
      if (rl.status === "fulfilled") setRlStatus(rl.value ?? {});
      if (opt.status === "fulfilled") setOptStatus(opt.value ?? {});
      if (appCfg.status === "fulfilled") {
        const ai_cfg = appCfg.value?.ai;
        if (ai_cfg) {
          setGateEnabled(Boolean(ai_cfg.confidence_filter_enabled));
          setGateThreshold(Number(ai_cfg.confidence_threshold ?? 60));
        }
      }
      const statsMap: Record<string, MemStats> = {};
      MODES.forEach((m, i) => {
        const r = mems[i];
        if (r.status === "fulfilled") statsMap[m] = r.value ?? {};
      });
      setMemStats(statsMap);
    } catch (_) {/* silently skip on network error */}
  }, []);

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

  // ── sub-components (inlined for brevity) ────────────────────────────────

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
        <span className="ml-1 text-gray-600">{active ? (current.dir === "asc" ? "↑" : "↓") : "↕"}</span>
      </th>
    );
  }

  // ── render ─────────────────────────────────────────────────────────────
  return (
    <main className="min-h-screen bg-gray-950 text-gray-200 p-6 space-y-10">
      <div>
        <h1 className="text-2xl font-bold text-white">AI / ML Brain</h1>
        <p className="text-sm text-gray-500 mt-1">
          Live model status, optimizer results, and trade memory — auto-refreshes every 30 s.
        </p>
      </div>

      {/* ── 1. LSTM Models ─────────────────────────────────────────────── */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <SectionHeader title="LSTM Prediction Models" sub="One model per symbol × trading mode, trained on OHLCV + indicators." />
          <button
            disabled={busy["train_all"]}
            onClick={() => doAction("train_all", () => trainAllSymbols())}
            className="px-3 py-1.5 text-xs bg-blue-700 hover:bg-blue-600 disabled:opacity-50 rounded font-medium"
          >
            {busy["train_all"] ? "Queuing…" : "Retrain All"}
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
                  placeholder="Search symbol or mode…"
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
                          <td className="py-2 pr-4">{m.accuracy != null ? pct(m.accuracy) : "—"}</td>
                          <td className="py-2 pr-4">{m.bars_used ?? "—"}</td>
                          <td className="py-2 pr-4 text-gray-400">{relTime(m.trained_at)}</td>
                          <td className="py-2 pr-4">
                            <Badge ok={!m.training} label={m.training ? "Training" : "Ready"} />
                          </td>
                          <td className="py-2">
                            <button
                              disabled={busy[bKey] || m.training}
                              onClick={() => doAction(bKey, () => trainSymbol(sym, type || "scalping"))}
                              className="px-2 py-1 text-xs bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded"
                            >
                              {busy[bKey] ? "…" : "Retrain"}
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

      {/* ── 2. RL Agents ───────────────────────────────────────────────── */}
      <section>
        <SectionHeader title="RL Agents" sub="Tabular Q-learning agent per trading mode — adjusts confidence threshold and risk factor over time." />
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {MODES.map((mode) => {
            const agent = rlStatus[mode] ?? {};
            return (
              <div key={mode} className="bg-gray-900 border border-gray-800 rounded-lg p-4 space-y-2">
                <p className="font-semibold text-white capitalize">{mode.replace("_", " ")}</p>
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
                  <div className="flex justify-between">
                    <span>Last state</span>
                    <span className="text-white font-medium">{agent.last_state ?? "—"}</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </section>

      {/* ── 3. Parameter Optimizer ─────────────────────────────────────── */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <SectionHeader title="Parameter Optimizer" sub="Walk-forward backtest grid search — runs automatically when win-rate drops below 45 % (24 h cooldown)." />
          <button
            disabled={busy["opt_all"]}
            onClick={() => doAction("opt_all", () => runOptimizerAll())}
            className="px-3 py-1.5 text-xs bg-purple-700 hover:bg-purple-600 disabled:opacity-50 rounded font-medium"
          >
            {busy["opt_all"] ? "Queuing…" : "Optimize All"}
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
                  placeholder="Search strategy or symbol…"
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
                          <td className="py-2 pr-4">{job.best_score != null ? job.best_score.toFixed(3) : "—"}</td>
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
                              {busy[bKey] ? "…" : "Run"}
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

      {/* ── 4. Trade Memory ────────────────────────────────────────────── */}
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

      {/* ── 0. AI Confidence Gate ──────────────────────────────────────── */}
      <section>
        <SectionHeader
          title="AI Confidence Gate"
          sub="Filter signals below a minimum confidence score. The RL agent always applies its own dynamic threshold regardless of this setting."
        />
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-5">

          {/* Toggle */}
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm text-white font-medium">Static confidence filter</p>
              <p className="text-xs text-gray-500 mt-0.5">
                When on, signals with confidence below the threshold are hard-blocked.
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
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                gateEnabled ? "bg-blue-600" : "bg-gray-700"
              }`}
            >
              <span className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                gateEnabled ? "translate-x-6" : "translate-x-1"
              }`} />
            </button>
          </div>

          {/* Threshold slider — only shown when gate is on */}
          {gateEnabled && (
            <div className="space-y-2">
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
              <p className="text-xs text-gray-500">
                Recommended: keep off while the LSTM is still learning (first 100 trades).
                Enable at 60–65% once you have steady win-rate data.
              </p>
            </div>
          )}

          {gateSaving && <p className="text-xs text-gray-500">Saving…</p>}

          {/* RL gate info — always active */}
          <div className="border-t border-gray-800 pt-4">
            <p className="text-sm text-white font-medium mb-1">RL dynamic gate</p>
            <p className="text-xs text-gray-500">
              Always active — starts at 55% and self-tunes between 40%–85% after each closed trade.
              Current thresholds are shown in the RL Agents section below.
            </p>
          </div>
        </div>
      </section>

      {/* ── 5. Auto-Training Schedule ──────────────────────────────────── */}
      <section>
        <SectionHeader title="Auto-Training Schedule" sub="Rules that trigger background retraining without any manual action." />
        <div className="bg-gray-900 border border-gray-800 rounded-lg p-5 space-y-3 text-sm text-gray-400">
          <div className="flex gap-3">
            <span className="text-blue-400 font-semibold w-48 shrink-0">LSTM retrain</span>
            <span>Every 20th closed trade per symbol × mode (runs in background thread, non-blocking).</span>
          </div>
          <div className="flex gap-3">
            <span className="text-purple-400 font-semibold w-48 shrink-0">Param optimizer</span>
            <span>Triggered when live win-rate drops below 45 % — with a 24 h cooldown per strategy × symbol. Walk-forward backtest tests up to 64 parameter combos.</span>
          </div>
          <div className="flex gap-3">
            <span className="text-amber-400 font-semibold w-48 shrink-0">RL agent</span>
            <span>Updates after every closed trade — adjusts confidence threshold and risk factor based on Q-value estimates.</span>
          </div>
          <div className="flex gap-3">
            <span className="text-emerald-400 font-semibold w-48 shrink-0">Best-strategy selector</span>
            <span>Per symbol, the strategy with the highest LSTM confidence (above threshold, R:R ≥ 1.5) wins. Runs at signal time — no extra schedule needed.</span>
          </div>
        </div>
      </section>
    </main>
  );
}
