"use client";
import { useState, useEffect, useCallback } from "react";
import dynamic from "next/dynamic";
import { fetchOHLCV, fetchPositions, fetchExecutionMode, setExecutionMode, fetchScannerConfig, patchScannerConfig, fetchAvailableSymbols } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import AccountSummary from "@/components/dashboard/AccountSummary";
import TradePanel from "@/components/trading/TradePanel";
import PositionTable from "@/components/trading/PositionTable";
import SignalQueue from "@/components/trading/SignalQueue";
import { IND_DEFAULTS, IND_LABEL, IND_COLOR, IND_GROUPS } from "@/components/chart/TradingChart";
import type { OHLCVBar, TradingMode, ExecutionMode } from "@/types";
import type { AllKey, CustomMA } from "@/components/chart/TradingChart";

// Dynamic import to prevent SSR for TradingView chart
const TradingChart = dynamic(() => import("@/components/chart/TradingChart"), {
  ssr: false,
  loading: () => <div className="w-full h-96 bg-gray-900 rounded-xl animate-pulse" />,
});

export interface SymbolGroup {
  label: string;
  symbols: string[];
}

interface Props {
  mode: TradingMode;
  label: string;
  icon: string;
  symbolGroups: SymbolGroup[];
  defaultSymbol: string;
  defaultTimeframe: string;
  timeframes: string[];
  strategyNames: string[];
}

export default function TradingModePage({
  mode,
  label,
  icon,
  symbolGroups,
  defaultSymbol,
  defaultTimeframe,
  timeframes,
  strategyNames,
}: Props) {
  const { account, positions, ticks, setPositions, pushNotification } = useBotStore();
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [timeframe, setTimeframe] = useState(defaultTimeframe);
  const [bars, setBars] = useState<OHLCVBar[]>([]);
  const [tab, setTab] = useState<"positions" | "signals">("signals");
  const [execMode, setExecMode] = useState<ExecutionMode>("manual");
  const [togglingExec, setTogglingExec] = useState(false);

  // Scanner state
  const [scannerEnabled, setScannerEnabled] = useState(false);
  const [scannerSymbols, setScannerSymbols] = useState<string[]>([]);
  const [scannerSaving, setScannerSaving] = useState(false);
  const [scannerSaved, setScannerSaved] = useState(false);
  const [availableSymbols, setAvailableSymbols] = useState<string[]>([]);
  const [symbolSearch, setSymbolSearch] = useState("");

  const [ind, setInd] = useState<Record<AllKey, boolean>>(IND_DEFAULTS);
  const [indPanelOpen, setIndPanelOpen] = useState(false);
  const toggleInd = (k: AllKey) => setInd((p) => ({ ...p, [k]: !p[k] }));

  const [customMAs, setCustomMAs] = useState<CustomMA[]>([]);
  const [cmaForm, setCmaForm] = useState<{ period: string; type: "ema" | "sma" }>({ period: "20", type: "ema" });
  const CMA_COLORS = ["#ec4899", "#8b5cf6", "#06b6d4", "#10b981", "#f59e0b", "#ef4444"];
  const addCustomMA = () => {
    const p = parseInt(cmaForm.period);
    if (isNaN(p) || p < 1 || p > 500) return;
    const usedColors = customMAs.map((m) => m.color);
    const color = CMA_COLORS.find((c) => !usedColors.includes(c)) ?? CMA_COLORS[customMAs.length % CMA_COLORS.length];
    setCustomMAs((prev) => [...prev, { id: `${cmaForm.type}_${p}_${Date.now()}`, period: p, type: cmaForm.type, color }]);
  };
  const removeCustomMA = (id: string) => setCustomMAs((prev) => prev.filter((m) => m.id !== id));

  // Auto-categorize symbols by type
  const categorizeSymbol = (sym: string): string => {
    const upper = sym.toUpperCase();
    
    // Forex pairs (6 chars ending with currency codes)
    const forexEndings = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"];
    if (sym.length === 6 && forexEndings.some(end => upper.endsWith(end))) {
      const majors = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"];
      return majors.includes(upper) ? "Forex Majors" : "Forex Minors";
    }
    
    // Crypto
    if (upper.includes("BTC") || upper.includes("ETH") || upper.includes("XRP") || 
        upper.includes("SOL") || upper.includes("ADA") || upper.includes("DOGE")) {
      return "Crypto";
    }
    
    // Indices
    if (upper.includes("US100") || upper.includes("US30") || upper.includes("US500") || 
        upper.includes("SPX") || upper.includes("NAS") || upper.includes("GER") || 
        upper.includes("UK100") || upper.includes("DAX")) {
      return "Indices";
    }
    
    // Commodities
    if (upper.includes("GOLD") || upper.includes("XAU") || upper.includes("SILVER") || 
        upper.includes("XAG") || upper.includes("OIL") || upper.includes("BRENT") || 
        upper.includes("NGAS")) {
      return "Commodities";
    }
    
    // Stocks (everything else)
    return "Stocks";
  };

  // Generate dynamic symbol groups from scanner symbols + hardcoded fallback
  const dynamicSymbolGroups: SymbolGroup[] = (() => {
    // Group scanner symbols by category
    const grouped: Record<string, string[]> = {};
    scannerSymbols.forEach(sym => {
      const category = categorizeSymbol(sym);
      if (!grouped[category]) grouped[category] = [];
      if (!grouped[category].includes(sym)) grouped[category].push(sym);
    });
    
    // Convert to SymbolGroup array
    const scannerGroups = Object.entries(grouped).map(([label, symbols]) => ({
      label,
      symbols: symbols.sort()
    }));
    
    // Merge with hardcoded groups (add missing symbols from hardcoded groups)
    const merged = [...scannerGroups];
    symbolGroups.forEach(hardcodedGroup => {
      const existingGroup = merged.find(g => g.label === hardcodedGroup.label);
      if (existingGroup) {
        // Add any missing symbols from hardcoded to existing group
        hardcodedGroup.symbols.forEach(sym => {
          if (!existingGroup.symbols.includes(sym)) {
            existingGroup.symbols.push(sym);
          }
        });
      } else {
        // Add entire hardcoded group if category doesn't exist
        merged.push(hardcodedGroup);
      }
    });
    
    // Sort groups by priority
    const priorityOrder = ["Forex Majors", "Forex Minors", "Crypto", "Commodities", "Indices", "Stocks"];
    return merged.sort((a, b) => {
      const aIdx = priorityOrder.indexOf(a.label);
      const bIdx = priorityOrder.indexOf(b.label);
      return (aIdx === -1 ? 999 : aIdx) - (bIdx === -1 ? 999 : bIdx);
    });
  })();

  const allSymbols = dynamicSymbolGroups.flatMap((g) => g.symbols);
  const modePrefix = mode === "scalping" ? "scalp" : mode === "day_trading" ? "day" : "swing";
  const modePositions = positions.filter((p) =>
    p.comment ? p.comment.startsWith(modePrefix) : allSymbols.includes(p.symbol)
  );

  const loadChart = useCallback(() => {
    fetchOHLCV(symbol, timeframe, 300)
      .then(setBars)
      .catch(() => {});
  }, [symbol, timeframe]);

  useEffect(() => {
    loadChart();
    const interval = setInterval(loadChart, 60_000);
    return () => clearInterval(interval);
  }, [loadChart]);

  // Load execution mode for this trading type
  useEffect(() => {
    fetchExecutionMode()
      .then((modes) => setExecMode((modes[mode] as ExecutionMode) ?? "manual"))
      .catch(() => {});
  }, [mode]);

  // Load scanner config for this mode
  useEffect(() => {
    fetchScannerConfig()
      .then((cfg) => {
        const m = cfg[mode] ?? {};
        setScannerEnabled(m.enabled ?? false);
        setScannerSymbols(m.symbols ?? []);
      })
      .catch(() => {});
    // Load live broker symbols once on mount
    fetchAvailableSymbols()
      .then((res) => setAvailableSymbols(res.symbols))
      .catch(() => {});
  }, [mode]);

  const SCANNER_TF: Record<string, string> = { scalping: "M1/M5", day_trading: "M15/H1", swing: "H4/D1" };
  const SCANNER_MAX: Record<string, number> = { scalping: 10, day_trading: 20, swing: 25 };

  const toggleScannerSymbol = (sym: string) => {
    setScannerSymbols((prev) => {
      if (prev.includes(sym)) return prev.filter((s) => s !== sym);
      if (prev.length >= (SCANNER_MAX[mode] ?? 5)) return prev;
      return [...prev, sym];
    });
  };

  const removeSymbol = (sym: string) => {
    setScannerSymbols((prev) => prev.filter((s) => s !== sym));
  };

  const clearAllSymbols = () => {
    if (scannerSymbols.length === 0) return;
    const count = scannerSymbols.length;
    setScannerSymbols([]);
    pushNotification({
      type: "warning",
      title: "Scanner Cleared",
      message: `Removed ${count} symbols from ${label} scanner`,
    });
  };

  const saveScannerConfig = async () => {
    setScannerSaving(true);
    try {
      await patchScannerConfig({
        [mode]: { enabled: scannerEnabled, symbols: scannerSymbols, timeframe: SCANNER_TF[mode] },
      });
      setScannerSaved(true);
      setTimeout(() => setScannerSaved(false), 2500);
      pushNotification({
        type: "success",
        title: `${label} scanner saved`,
        message: scannerEnabled
          ? `Scanning: ${scannerSymbols.join(", ") || "none"}`
          : "Scanner paused.",
      });
    } catch {
      pushNotification({ type: "error", title: "Save failed", message: "Could not update scanner config." });
    } finally {
      setScannerSaving(false);
    }
  };

  const toggleExecMode = async () => {
    setTogglingExec(true);
    const next: ExecutionMode = execMode === "manual" ? "auto" : "manual";
    const ok = window.confirm(
      next === "auto"
        ? `Enable AUTO execution for ${label}? Orders will fire without confirmation.`
        : `Switch ${label} back to MANUAL confirmation?`
    );
    if (ok) {
      try {
        await setExecutionMode(mode, next);
        setExecMode(next);
        pushNotification({
          type: next === "auto" ? "warning" : "success",
          title: `${label}: ${next.toUpperCase()} mode`,
          message: next === "auto" ? "Orders will execute automatically." : "Manual confirmation required.",
        });
      } catch {
        pushNotification({ type: "error", title: "Mode switch failed", message: "" });
      }
    }
    setTogglingExec(false);
  };

  const refreshPositions = () =>
    fetchPositions().then(setPositions).catch(() => {});

  const tick = ticks[symbol];

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="text-3xl">{icon}</span>
          <div>
            <h2 className="text-2xl font-bold text-white">{label}</h2>
            <p className="text-gray-500 text-sm">
              {strategyNames.join(" · ")}
            </p>
          </div>
        </div>

        {/* Execution mode badge + toggle */}
        <button
          onClick={toggleExecMode}
          disabled={togglingExec}
          className={`flex items-center gap-2 px-4 py-2 rounded-full text-xs font-bold transition-colors ${
            execMode === "auto"
              ? "bg-orange-900 text-orange-300 hover:bg-orange-800 border border-orange-700"
              : "bg-gray-800 text-gray-400 hover:bg-gray-700 border border-gray-700"
          }`}
        >
          <span className={`w-2 h-2 rounded-full ${execMode === "auto" ? "bg-orange-400 animate-pulse" : "bg-gray-500"}`} />
          {execMode === "auto" ? "AUTO" : "MANUAL"}
        </button>
      </div>

      <AccountSummary account={account} />

      {/* ── Strategy Scanner ─────────────────────────────────────────── */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
        <div className="flex items-center justify-between mb-4">
          <div>
            <p className="text-sm font-semibold text-white">Strategy Scanner</p>
            <p className="text-xs text-gray-500 mt-0.5">
              {SCANNER_TF[mode]}&nbsp;&middot;&nbsp;max {SCANNER_MAX[mode] ?? 5} symbols
            </p>
          </div>
          <div className="flex items-center gap-3">
            <span
              className={`text-xs font-semibold ${
                scannerEnabled ? "text-emerald-400" : "text-gray-500"
              }`}
            >
              {scannerEnabled ? "ACTIVE" : "PAUSED"}
            </span>
            <button
              onClick={() => setScannerEnabled((v) => !v)}
              className={`inline-flex w-11 h-6 items-center rounded-full transition-colors ${
                scannerEnabled ? "bg-emerald-600" : "bg-gray-700"
              }`}
            >
              <span
                className={`inline-block w-4 h-4 rounded-full bg-white transition-transform transform ${
                  scannerEnabled ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>
        </div>

        {/* Currently Scanning Symbols */}
        {scannerSymbols.length > 0 && (
          <div className="mb-4 p-3 bg-gray-800/50 rounded-lg border border-gray-700">
            <div className="flex items-center justify-between mb-2">
              <p className="text-xs font-medium text-gray-400">
                Currently Scanning ({scannerSymbols.length}/{SCANNER_MAX[mode] ?? 5})
              </p>
              <button
                onClick={clearAllSymbols}
                className="text-xs font-medium text-red-400 hover:text-red-300 transition-colors"
              >
                Clear All
              </button>
            </div>
            <div className="flex flex-wrap gap-2">
              {scannerSymbols.map((sym) => (
                <div
                  key={sym}
                  className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-blue-600 border border-blue-500 text-white text-xs font-medium"
                >
                  <span>{sym}</span>
                  <button
                    onClick={() => removeSymbol(sym)}
                    className="hover:bg-blue-700 rounded-full p-0.5 transition-colors"
                  >
                    <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Available Symbols to Add — searchable live list from broker */}
        <div className="mb-4">
          <p className="text-xs font-medium text-gray-400 mb-2">
            {scannerSymbols.length === 0 ? "Select symbols to scan" : "Add more symbols"}
          </p>
          <input
            type="text"
            placeholder="Search symbols (e.g. EURUSD, GOLD, BTC)…"
            value={symbolSearch}
            onChange={(e) => setSymbolSearch(e.target.value)}
            className="w-full px-3 py-2 mb-2 bg-gray-800 border border-gray-700 rounded-lg text-xs text-gray-100 placeholder-gray-500 focus:outline-none focus:border-blue-500 transition-colors"
          />
          <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto pr-1">
            {(symbolSearch.trim().length > 0
              ? availableSymbols.filter(
                  (s) =>
                    !scannerSymbols.includes(s) &&
                    s.toLowerCase().includes(symbolSearch.trim().toLowerCase())
                )
              : allSymbols.filter((s) => !scannerSymbols.includes(s))
            ).map((sym) => {
              const atMax = scannerSymbols.length >= (SCANNER_MAX[mode] ?? 5);
              return (
                <button
                  key={sym}
                  disabled={atMax}
                  onClick={() => toggleScannerSymbol(sym)}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-colors border ${
                    atMax
                      ? "bg-gray-900 border-gray-800 text-gray-700 cursor-not-allowed"
                      : "bg-gray-800 border-gray-700 text-gray-400 hover:text-white hover:border-gray-600"
                  }`}
                >
                  + {sym}
                </button>
              );
            })}
            {symbolSearch.trim().length > 0 &&
              availableSymbols.filter(
                (s) =>
                  !scannerSymbols.includes(s) &&
                  s.toLowerCase().includes(symbolSearch.trim().toLowerCase())
              ).length === 0 && (
                <p className="text-xs text-gray-600 italic">No matching symbols found</p>
              )}
          </div>
        </div>

        <div className="flex gap-2">
          <button
            disabled={scannerSaving}
            onClick={saveScannerConfig}
            className={`px-4 py-2 rounded-lg text-xs font-medium transition-colors flex-1 ${
              scannerSaved
                ? "bg-emerald-700 text-emerald-100"
                : "bg-blue-600 hover:bg-blue-500 text-white"
            } disabled:opacity-50`}
          >
            {scannerSaving ? "Saving…" : scannerSaved ? "✓ Saved" : "Save Scanner"}
          </button>
        </div>
      </div>

      {/* Symbol + timeframe controls */}
      <div className="flex flex-wrap gap-3 items-center">
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
        >
          {dynamicSymbolGroups.map((grp) => (
            <optgroup key={grp.label} label={grp.label}>
              {grp.symbols.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </optgroup>
          ))}
        </select>

        <div className="flex gap-1">
          {timeframes.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
                timeframe === tf
                  ? "bg-blue-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-white"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        {/* Indicator chips */}
        <div className="flex items-center gap-1 flex-wrap">
          {(Object.keys(ind) as AllKey[]).filter((k) => ind[k]).map((k) => (
            <button
              key={k}
              onClick={() => toggleInd(k)}
              className="flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors"
              style={{ backgroundColor: IND_COLOR[k] + "20", borderColor: IND_COLOR[k] + "80", color: IND_COLOR[k] }}
            >
              {IND_LABEL[k]} <span className="opacity-50 ml-0.5">×</span>
            </button>
          ))}
          {customMAs.map((m) => (
            <button
              key={m.id}
              onClick={() => removeCustomMA(m.id)}
              className="flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium border transition-colors"
              style={{ backgroundColor: m.color + "20", borderColor: m.color + "80", color: m.color }}
            >
              {m.type.toUpperCase()}({m.period}) <span className="opacity-50 ml-0.5">×</span>
            </button>
          ))}
          <button
            onClick={() => setIndPanelOpen((v) => !v)}
            className="px-1.5 py-0.5 rounded text-[10px] text-gray-500 border border-dashed border-gray-700 hover:text-white hover:border-gray-500 transition-colors"
          >
            + Add
          </button>
        </div>

        {tick && (
          <div className="ml-auto flex gap-4 text-sm">
            <span className="text-gray-500">Bid</span>
            <span className="text-white font-mono">{tick.bid.toFixed(5)}</span>
            <span className="text-gray-500">Ask</span>
            <span className="text-white font-mono">{tick.ask.toFixed(5)}</span>
          </div>
        )}
      </div>

      {/* Indicator selector panel */}
      {indPanelOpen && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="grid grid-cols-2 md:grid-cols-4">
            {IND_GROUPS.map((grp, gi) => (
              <div key={grp.id} className={["px-4 py-4", gi < IND_GROUPS.length - 1 ? "border-r border-gray-800" : ""].join(" ")}>
                <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest mb-3">{grp.label}</p>
                <div className="space-y-2">
                  {grp.keys.map((k) => (
                    <button key={k} onClick={() => toggleInd(k)} className="flex items-center gap-2 w-full text-left group">
                      <div
                        className="w-3.5 h-3.5 rounded border-2 shrink-0 flex items-center justify-center transition-all"
                        style={ind[k] ? { backgroundColor: IND_COLOR[k] + "cc", borderColor: IND_COLOR[k] } : { borderColor: "#475569" }}
                      >
                        {ind[k] && <span className="text-white leading-none font-black" style={{ fontSize: 8 }}>✓</span>}
                      </div>
                      <span className="text-xs transition-colors group-hover:text-white" style={ind[k] ? { color: IND_COLOR[k] } : { color: "#64748b" }}>
                        {IND_LABEL[k]}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            ))}
          </div>
          {/* Custom MA builder */}
          <div className="border-t border-gray-800 px-4 py-3 flex items-center gap-3 flex-wrap">
            <p className="text-[10px] font-bold text-gray-500 uppercase tracking-widest shrink-0">Custom MA</p>
            <input
              type="number"
              placeholder="Period"
              value={cmaForm.period}
              min={1} max={500}
              onChange={(e) => setCmaForm((f) => ({ ...f, period: e.target.value }))}
              className="w-16 bg-gray-800 border border-gray-700 rounded px-2 py-1 text-xs text-white focus:outline-none focus:border-blue-500"
            />
            <div className="flex rounded overflow-hidden border border-gray-700">
              {(["ema", "sma"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setCmaForm((f) => ({ ...f, type: t }))}
                  className={`px-2 py-1 text-[10px] font-medium transition-colors ${
                    cmaForm.type === t ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-500 hover:text-white"
                  }`}
                >
                  {t.toUpperCase()}
                </button>
              ))}
            </div>
            <button
              onClick={addCustomMA}
              className="px-2 py-1 text-[10px] rounded bg-blue-700 hover:bg-blue-600 text-white font-medium transition-colors"
            >
              + Add
            </button>
          </div>
        </div>
      )}

      {/* Chart + right panel */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* Chart */}
        <div className="xl:col-span-3 bg-gray-900 border border-gray-800 rounded-xl p-4">
          <TradingChart
            bars={bars}
            height={420}
            positions={positions.filter((p) => p.symbol === symbol)}
            ind={ind}
            onToggle={toggleInd}
            customMAs={customMAs}
          />
        </div>

        {/* Trade panel */}
        <div className="xl:col-span-1">
          <TradePanel mode={mode} defaultSymbol={symbol} symbolGroups={symbolGroups} execMode={execMode} />
        </div>
      </div>

      {/* Positions + Signals tabs */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex gap-4 mb-5 border-b border-gray-800 pb-3">
          {(["signals", "positions"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`text-sm font-medium capitalize transition-colors ${
                tab === t ? "text-white" : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {t === "positions"
                ? `Positions (${modePositions.length})`
                : "Signal Queue"}
            </button>
          ))}
        </div>

        {tab === "positions" ? (
          <PositionTable positions={modePositions} onRefresh={refreshPositions} />
        ) : (
          <SignalQueue mode={mode} />
        )}
      </div>
    </div>
  );
}
