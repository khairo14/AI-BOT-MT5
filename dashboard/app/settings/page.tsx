"use client";
import { useEffect, useState, useCallback } from "react";
import {
  fetchRiskConfig,
  fetchAppConfig,
  fetchAccountMode,
  fetchExecutionMode,
  setExecutionMode,
  patchRiskConfig,
  patchAppConfig,
  switchMode,
  fetchRiskStatus,
  resetDrawdown,
  resetConsecutiveLosses,
  toggleCircuitBreaker,
  fetchHealth,
  reconnectMT5,
} from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { TradingMode, ExecutionMode } from "@/types";

// ── helpers ───────────────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
      <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">{title}</h3>
      {children}
    </div>
  );
}

function NumField({
  label,
  value,
  onChange,
  min,
  max,
  step,
  unit,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
}) {
  return (
    <div className="flex items-center justify-between gap-4">
      <label className="text-sm text-gray-400 flex-1">{label}</label>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={value}
          onChange={(e) => onChange(parseFloat(e.target.value))}
          min={min}
          max={max}
          step={step ?? 0.1}
          className="w-24 bg-gray-800 border border-gray-700 rounded-lg px-3 py-1.5 text-sm text-white text-right focus:outline-none focus:border-blue-500"
        />
        {unit && <span className="text-xs text-gray-500 w-6">{unit}</span>}
      </div>
    </div>
  );
}

function SaveBtn({ onClick, saving, saved }: { onClick: () => void; saving: boolean; saved: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={saving}
      className={`mt-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
        saved
          ? "bg-emerald-700 text-emerald-100"
          : "bg-blue-600 hover:bg-blue-500 text-white"
      } disabled:opacity-50`}
    >
      {saving ? "Saving…" : saved ? "✓ Saved" : "Save Changes"}
    </button>
  );
}

const EXEC_MODES: { mode: TradingMode; label: string }[] = [
  { mode: "scalping",    label: "Scalping" },
  { mode: "day_trading", label: "Day Trading" },
  { mode: "swing",       label: "Swing" },
];

// ── main component ────────────────────────────────────────────────────────

export default function SettingsPage() {
  const { pushNotification } = useBotStore();

  // Execution modes
  const [execModes, setExecModes] = useState<Record<string, string>>({
    scalping: "manual", day_trading: "manual", swing: "manual",
  });
  const [execSaving, setExecSaving] = useState(false);
  const [execSaved, setExecSaved] = useState(false);

  // Risk config
  const [risk, setRisk] = useState<Record<string, unknown> | null>(null);
  const [riskSaving, setRiskSaving] = useState(false);
  const [riskSaved, setRiskSaved] = useState(false);

  // App / trading mode
  const [tradingMode, setTradingMode] = useState<"paper" | "live">("paper");
  const [newsFilter, setNewsFilter] = useState(true);
  const [sessionFilter, setSessionFilter] = useState(true);
  const [appSaving, setAppSaving] = useState(false);
  const [appSaved, setAppSaved] = useState(false);

  // Circuit breaker live status
  const [cbStatus, setCbStatus] = useState<{
    circuit_breaker_enabled: boolean;
    daily_halted: boolean;
    weekly_halted: boolean;
    consecutive_losses: Record<string, number>;
    paused_modes: Record<string, string | null>;
  } | null>(null);
  const [cbBusy, setCbBusy] = useState(false);

  // MT5 connection
  const [mt5Connected, setMt5Connected] = useState<boolean | null>(null);
  const [reconnecting, setReconnecting] = useState(false);

  const load = useCallback(async () => {
    try {
      const [exec, riskData, appData, modeData, cbData, healthData] = await Promise.all([
        fetchExecutionMode(),
        fetchRiskConfig(),
        fetchAppConfig(),
        fetchAccountMode(),
        fetchRiskStatus(),
        fetchHealth(),
      ]);
      setExecModes(exec);
      setRisk(riskData as unknown as Record<string, unknown>);
      // Use actual MT5 connection mode, not app.json (which may be stale)
      setTradingMode((modeData as { mode: "paper" | "live" }).mode ?? "paper");
      const nf = (riskData as unknown as Record<string, unknown>).news_filter as Record<string, unknown> | undefined;
      const sf = (riskData as unknown as Record<string, unknown>).session_filter as Record<string, unknown> | undefined;
      setNewsFilter((nf?.enabled as boolean) ?? true);
      setSessionFilter((sf?.enabled as boolean) ?? true);
      setCbStatus(cbData);
      setMt5Connected((healthData as { mt5_connected: boolean }).mt5_connected ?? false);
    } catch {
      pushNotification({ type: "error", title: "Settings load failed", message: "Could not reach the API." });
    }
  }, [pushNotification]);

  useEffect(() => { load(); }, [load]);

  // ── save handlers ──────────────────────────────────────────────────────

  const saveExecModes = async () => {
    setExecSaving(true);
    try {
      await Promise.all(
        EXEC_MODES.map(({ mode }) =>
          setExecutionMode(mode, execModes[mode] as ExecutionMode)
        )
      );
      setExecSaved(true);
      setTimeout(() => setExecSaved(false), 2500);
      pushNotification({ type: "success", title: "Execution modes saved", message: "" });
    } catch {
      pushNotification({ type: "error", title: "Save failed", message: "Could not update execution modes." });
    } finally {
      setExecSaving(false);
    }
  };

  const saveRisk = async () => {
    if (!risk) return;
    setRiskSaving(true);
    try {
      await patchRiskConfig(risk as never);
      setRiskSaved(true);
      setTimeout(() => setRiskSaved(false), 2500);
      pushNotification({ type: "success", title: "Risk config saved", message: "" });
    } catch {
      pushNotification({ type: "error", title: "Save failed", message: "Could not update risk config." });
    } finally {
      setRiskSaving(false);
    }
  };

  const saveApp = async () => {
    setAppSaving(true);
    try {
      // Switch MT5 account mode via the proper endpoint (not just app.json)
      const currentMode = await fetchAccountMode();
      if (currentMode.mode !== tradingMode) {
        await switchMode(tradingMode, false);
      }
      await patchRiskConfig({
        news_filter: { enabled: newsFilter },
        session_filter: { enabled: sessionFilter },
      } as never);
      setAppSaved(true);
      setTimeout(() => setAppSaved(false), 2500);
      pushNotification({ type: "success", title: "App settings saved", message: "" });
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: { message?: string } | string } } })?.response?.data?.detail;
      const msg = typeof detail === "object" ? detail?.message : (detail as string) ?? "Could not update app settings.";
      pushNotification({ type: "error", title: "Save failed", message: msg ?? "Could not update app settings." });
    } finally {
      setAppSaving(false);
    }
  };

  // ── risk field helpers ─────────────────────────────────────────────────

  const riskNum = (key: string): number =>
    (risk?.[key] as number) ?? 0;
  const setRiskKey = (key: string, val: number) =>
    setRisk((r) => ({ ...(r ?? {}), [key]: val }));

  const drawdown = (risk?.drawdown ?? {}) as Record<string, unknown>;
  const setDrawdown = (key: string, val: number) =>
    setRisk((r) => ({
      ...(r ?? {}),
      drawdown: { ...(drawdown), [key]: val },
    }));

  const maxTrades = (risk?.max_concurrent_trades ?? {}) as Record<string, number>;
  const setMaxTrades = (key: string, val: number) =>
    setRisk((r) => ({
      ...(r ?? {}),
      max_concurrent_trades: { ...maxTrades, [key]: val },
    }));

  if (!risk) {
    return (
      <div className="p-8">
        <p className="text-gray-500 text-sm">Loading settings…</p>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-3xl space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-white">Settings</h2>
        <p className="text-gray-500 text-sm mt-1">
          Runtime configuration — changes take effect immediately without restart.
        </p>
      </div>

      {/* ── MT5 Connection ───────────────────────────────────────────── */}
      <Section title="MT5 Connection">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={`w-2.5 h-2.5 rounded-full ${mt5Connected === null ? "bg-gray-500" : mt5Connected ? "bg-emerald-400" : "bg-red-500"}`} />
            <span className="text-sm text-gray-300">
              {mt5Connected === null ? "Checking…" : mt5Connected ? "Connected" : "Disconnected"}
            </span>
          </div>
          <button
            disabled={reconnecting}
            onClick={async () => {
              setReconnecting(true);
              try {
                await reconnectMT5();
                const h = await fetchHealth();
                setMt5Connected(h.mt5_connected);
                pushNotification({ type: "success", title: "MT5 reconnected", message: "" });
              } catch {
                const h = await fetchHealth().catch(() => ({ mt5_connected: false }));
                setMt5Connected(h.mt5_connected);
                pushNotification({ type: "error", title: "Reconnect failed", message: "Check the MT5 terminal is running." });
              } finally {
                setReconnecting(false);
              }
            }}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-blue-700 hover:bg-blue-600 text-white disabled:opacity-40 transition-colors"
          >
            {reconnecting ? "Reconnecting…" : "Reconnect"}
          </button>
        </div>
      </Section>

      {/* ── Circuit Breaker ──────────────────────────────────────────── */}
      <Section title="Circuit Breaker">
        <p className="text-xs text-gray-500">
          Reset halts manually or disable the circuit breaker entirely for testing.
          Changes take effect immediately.
        </p>

        {/* Enable / Disable toggle */}
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-400">Circuit breaker enabled</span>
          <button
            disabled={cbBusy || cbStatus === null}
            onClick={async () => {
              setCbBusy(true);
              try {
                const next = !(cbStatus?.circuit_breaker_enabled ?? true);
                await toggleCircuitBreaker(next);
                setCbStatus((s) => s ? { ...s, circuit_breaker_enabled: next } : s);
                pushNotification({ type: "success", title: `Circuit breaker ${next ? "enabled" : "disabled"}`, message: "" });
              } catch {
                pushNotification({ type: "error", title: "Failed", message: "Could not toggle circuit breaker." });
              } finally {
                setCbBusy(false);
              }
            }}
            className={`inline-flex w-11 h-6 items-center rounded-full transition-colors disabled:opacity-40 ${`
              (cbStatus?.circuit_breaker_enabled ?? true) ? "bg-blue-600" : "bg-gray-700"
            }`}
          >
            <span
              className={`inline-block w-4 h-4 rounded-full bg-white transition-transform transform ${
                (cbStatus?.circuit_breaker_enabled ?? true) ? "translate-x-6" : "translate-x-1"
              }`}
            />
          </button>
        </div>

        {/* Status badges */}
        {cbStatus && (
          <div className="space-y-2 pt-1">
            <div className="flex flex-wrap gap-2 text-xs">
              <span className={`px-2 py-1 rounded font-semibold ${cbStatus.daily_halted ? "bg-red-900/50 text-red-300" : "bg-gray-800 text-gray-500"}`}>
                Daily halt: {cbStatus.daily_halted ? "ACTIVE" : "clear"}
              </span>
              <span className={`px-2 py-1 rounded font-semibold ${cbStatus.weekly_halted ? "bg-orange-900/50 text-orange-300" : "bg-gray-800 text-gray-500"}`}>
                Weekly halt: {cbStatus.weekly_halted ? "ACTIVE" : "clear"}
              </span>
              {Object.entries(cbStatus.paused_modes).map(([mode, ts]) =>
                ts ? (
                  <span key={mode} className="px-2 py-1 rounded font-semibold bg-yellow-900/50 text-yellow-300">
                    {mode} paused
                  </span>
                ) : null
              )}
            </div>
            <div className="flex flex-wrap gap-2 text-xs text-gray-500">
              {Object.entries(cbStatus.consecutive_losses).map(([mode, n]) => (
                <span key={mode}>{mode}: {n} loss{n !== 1 ? "es" : ""}</span>
              ))}
            </div>
          </div>
        )}

        {/* Action buttons */}
        <div className="flex gap-3 pt-1 flex-wrap">
          <button
            disabled={cbBusy || cbStatus === null}
            onClick={async () => {
              setCbBusy(true);
              try {
                await resetDrawdown();
                const updated = await fetchRiskStatus();
                setCbStatus(updated);
                pushNotification({ type: "success", title: "Drawdown reset", message: "Daily & weekly halts cleared." });
              } catch {
                pushNotification({ type: "error", title: "Reset failed", message: "" });
              } finally { setCbBusy(false); }
            }}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-amber-700 hover:bg-amber-600 text-white disabled:opacity-40 transition-colors"
          >
            Reset Drawdown Halts
          </button>
          <button
            disabled={cbBusy || cbStatus === null}
            onClick={async () => {
              setCbBusy(true);
              try {
                await resetConsecutiveLosses();
                const updated = await fetchRiskStatus();
                setCbStatus(updated);
                pushNotification({ type: "success", title: "Consecutive losses reset", message: "All mode pauses cleared." });
              } catch {
                pushNotification({ type: "error", title: "Reset failed", message: "" });
              } finally { setCbBusy(false); }
            }}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-gray-700 hover:bg-gray-600 text-white disabled:opacity-40 transition-colors"
          >
            Reset Consecutive Losses
          </button>
        </div>
      </Section>

      {/* ── Execution Modes ──────────────────────────────────────────── */}
      <Section title="Execution Mode">
        <p className="text-xs text-gray-500">
          Manual: signals queue for your approval. Auto: orders fire immediately.
        </p>
        <div className="space-y-3">
          {EXEC_MODES.map(({ mode, label }) => (
            <div key={mode} className="flex items-center justify-between">
              <span className="text-sm text-gray-300">{label}</span>
              <div className="flex rounded-lg overflow-hidden border border-gray-700">
                {(["manual", "auto"] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setExecModes((prev) => ({ ...prev, [mode]: m }))}
                    className={`px-4 py-1.5 text-xs font-medium transition-colors ${
                      execModes[mode] === m
                        ? m === "auto"
                          ? "bg-orange-600 text-white"
                          : "bg-blue-600 text-white"
                        : "bg-gray-800 text-gray-500 hover:text-white"
                    }`}
                  >
                    {m.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
        <SaveBtn onClick={saveExecModes} saving={execSaving} saved={execSaved} />
      </Section>

      {/* ── Per-Trade Risk ────────────────────────────────────────────── */}
      <Section title="Per-Trade Risk">
        <NumField
          label="Risk per trade"
          value={riskNum("risk_per_trade_pct")}
          onChange={(v) => setRiskKey("risk_per_trade_pct", v)}
          min={0.1} max={5} step={0.1} unit="%"
        />
        <NumField
          label="Max risk per trade (hard cap)"
          value={riskNum("max_risk_per_trade_pct")}
          onChange={(v) => setRiskKey("max_risk_per_trade_pct", v)}
          min={0.5} max={10} step={0.1} unit="%"
        />
        <NumField
          label="Minimum R:R ratio"
          value={riskNum("risk_reward_min")}
          onChange={(v) => setRiskKey("risk_reward_min", v)}
          min={1} max={5} step={0.1} unit=":1"
        />
        <SaveBtn onClick={saveRisk} saving={riskSaving} saved={riskSaved} />
      </Section>

      {/* ── Concurrent Trade Limits ───────────────────────────────────── */}
      <Section title="Concurrent Trade Limits">
        {(["scalping", "day_trading", "swing", "total"] as const).map((k) => (
          <NumField
            key={k}
            label={k === "total" ? "Total (all modes)" : `${k.replace("_", " ")} max`}
            value={maxTrades[k] ?? 0}
            onChange={(v) => setMaxTrades(k, Math.round(v))}
            min={1} max={30} step={1} unit=""
          />
        ))}
        <NumField
          label="Max trades per symbol"
          value={maxTrades["per_symbol"] ?? 1}
          onChange={(v) => setMaxTrades("per_symbol", Math.round(v))}
          min={1} max={5} step={1} unit=""
        />
        <SaveBtn onClick={saveRisk} saving={riskSaving} saved={riskSaved} />
      </Section>

      {/* ── Drawdown Protection ───────────────────────────────────────── */}
      <Section title="Drawdown Protection">
        <NumField
          label="Daily drawdown limit"
          value={(drawdown.daily_limit_pct as number) ?? 5}
          onChange={(v) => setDrawdown("daily_limit_pct", v)}
          min={1} max={20} step={0.5} unit="%"
        />
        <NumField
          label="Weekly drawdown limit"
          value={(drawdown.weekly_limit_pct as number) ?? 10}
          onChange={(v) => setDrawdown("weekly_limit_pct", v)}
          min={2} max={40} step={0.5} unit="%"
        />
        <NumField
          label="Max consecutive losses before pause"
          value={(drawdown.max_consecutive_losses as number) ?? 5}
          onChange={(v) => setDrawdown("max_consecutive_losses", Math.round(v))}
          min={1} max={20} step={1} unit=""
        />
        <NumField
          label="Pause duration after consecutive losses"
          value={(drawdown.consecutive_loss_pause_hours as number) ?? 4}
          onChange={(v) => setDrawdown("consecutive_loss_pause_hours", Math.round(v))}
          min={1} max={24} step={1} unit="hr"
        />
        <SaveBtn onClick={saveRisk} saving={riskSaving} saved={riskSaved} />
      </Section>

      {/* ── Filters ───────────────────────────────────────────────────── */}
      <Section title="Filters">
        <div className="space-y-3">
          {[
            { label: "News filter (pause before/after high-impact events)", value: newsFilter, set: setNewsFilter },
            { label: "Session filter (suppress signals outside market hours)", value: sessionFilter, set: setSessionFilter },
          ].map(({ label, value, set }) => (
            <div key={label} className="flex items-center justify-between">
              <span className="text-sm text-gray-400 flex-1">{label}</span>
              <button
                onClick={() => set(!value)}
                className={`inline-flex w-11 h-6 items-center rounded-full transition-colors ${value ? "bg-blue-600" : "bg-gray-700"}`}
              >
                <span
                  className={`inline-block w-4 h-4 rounded-full bg-white transition-transform transform ${value ? "translate-x-6" : "translate-x-1"}`}
                />
              </button>
            </div>
          ))}
        </div>

        <div className="flex items-center justify-between pt-2">
          <span className="text-sm text-gray-400">Active trading mode</span>
          <div className="flex rounded-lg overflow-hidden border border-gray-700">
            {(["paper", "live"] as const).map((m) => (
              <button
                key={m}
                onClick={() => setTradingMode(m)}
                className={`px-4 py-1.5 text-xs font-medium transition-colors ${
                  tradingMode === m
                    ? m === "live"
                      ? "bg-red-600 text-white"
                      : "bg-emerald-700 text-white"
                    : "bg-gray-800 text-gray-500 hover:text-white"
                }`}
              >
                {m.toUpperCase()}
              </button>
            ))}
          </div>
        </div>
        <p className="text-xs text-amber-600">
          ⚠ To switch accounts with open positions, use the Account Switcher in the sidebar.
        </p>
        <SaveBtn onClick={saveApp} saving={appSaving} saved={appSaved} />
      </Section>
    </div>
  );
}
