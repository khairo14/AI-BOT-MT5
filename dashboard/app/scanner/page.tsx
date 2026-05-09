"use client";

import { useEffect, useState, useCallback } from "react";
import {
  fetchScanResults,
  fetchScanByType,
  triggerScan,
  addSymbolToConfig,
  invalidateScannerCache,
  fetchScannerHealth,
  fetchScannerConfig,
  patchScannerConfig,
  type ScanResult,
  type ScanSummary,
} from "@/lib/api";
import { useBotStore } from "@/lib/store";
import ScannerMonitor from "@/components/dashboard/ScannerMonitor";
import MarketAnalysisTab from "@/components/dashboard/MarketAnalysisTab";

// Mode capacity limits
const MODE_LIMITS: Record<string, number> = {
  scalping: 10,
  day_trading: 20,
  swing: 25,
};

// ── helpers ────────────────────────────────────────────────────────────────
function relTime(iso: string | undefined): string {
  if (!iso) return "—";
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

function scoreColor(score: number): string {
  if (score >= 70) return "text-green-400";
  if (score >= 50) return "text-yellow-400";
  return "text-red-400";
}

function scoreBg(score: number): string {
  if (score >= 70) return "bg-green-500/20 border-green-500/30";
  if (score >= 50) return "bg-yellow-500/20 border-yellow-500/30";
  return "bg-red-500/20 border-red-500/30";
}

const TRADING_TYPES = ["scalping", "day_trading", "swing"] as const;
const TYPE_LABELS: Record<string, string> = {
  scalping: "Scalping",
  day_trading: "Day Trading",
  swing: "Swing",
};

// ── component ──────────────────────────────────────────────────────────────
export default function ScannerPage() {
  const { pushNotification } = useBotStore();
  const [activeTab, setActiveTab] = useState<"monitor" | "analysis" | "scalping" | "day_trading" | "swing">("monitor");
  const [scanData, setScanData] = useState<ScanSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [addingSymbol, setAddingSymbol] = useState<string | null>(null);
  const [searchTerm, setSearchTerm] = useState("");
  const [sortKey, setSortKey] = useState<keyof ScanResult>("composite_score");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [health, setHealth] = useState<any>(null);
  const [activeSymbols, setActiveSymbols] = useState<Record<string, string[]>>({
    scalping: [],
    day_trading: [],
    swing: [],
  });

  // Load scanner config (active symbols per mode)
  const loadActiveSymbols = useCallback(async () => {
    try {
      const cfg = await fetchScannerConfig();
      setActiveSymbols({
        scalping: cfg.scalping?.symbols || [],
        day_trading: cfg.day_trading?.symbols || [],
        swing: cfg.swing?.symbols || [],
      });
    } catch (err) {
      console.error("Failed to load scanner config:", err);
    }
  }, []);

  // Load initial data
  const loadData = useCallback(async (forceRefresh = false) => {
    try {
      setLoading(true);
      const res = await fetchScanResults(forceRefresh);
      setScanData(res.data);
    } catch (err) {
      console.error("Failed to load scan results:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadHealth = useCallback(async () => {
    try {
      const res = await fetchScannerHealth();
      setHealth(res);
    } catch (err) {
      console.error("Failed to load scanner health:", err);
    }
  }, []);

  useEffect(() => {
    loadData();
    loadHealth();
    loadActiveSymbols();
    const interval = setInterval(() => {
      loadData(); // Auto-refresh every 5 minutes
      loadHealth();
      loadActiveSymbols();
    }, 300_000);
    return () => clearInterval(interval);
  }, [loadData, loadHealth, loadActiveSymbols]);

  // Manual scan trigger
  const handleScan = async (tradingType?: string) => {
    setScanning(true);
    try {
      await triggerScan(tradingType, true);
      await loadData(false);
      await loadHealth();
    } catch (err) {
      console.error("Scan failed:", err);
    } finally {
      setScanning(false);
    }
  };

  // Add symbol to config
  const handleAddSymbol = async (symbol: string, tradingType: string) => {
    // Check if already at capacity
    const currentSymbols = activeSymbols[tradingType] || [];
    const limit = MODE_LIMITS[tradingType];
    
    if (currentSymbols.length >= limit) {
      pushNotification({
        type: "warning",
        title: "Capacity Reached",
        message: `${TYPE_LABELS[tradingType]} scanner is full (${limit}/${limit} symbols). Remove a symbol first.`,
      });
      return;
    }

    setAddingSymbol(symbol);
    try {
      await addSymbolToConfig(symbol, tradingType, true);
      await loadActiveSymbols(); // Reload active symbols
      pushNotification({
        type: "success",
        title: "Symbol Added",
        message: `${symbol} added to ${TYPE_LABELS[tradingType]} (${currentSymbols.length + 1}/${limit})`,
      });
    } catch (err) {
      console.error("Failed to add symbol:", err);
      pushNotification({
        type: "error",
        title: "Failed to Add Symbol",
        message: `Could not add ${symbol} to ${TYPE_LABELS[tradingType]}`,
      });
    } finally {
      setAddingSymbol(null);
    }
  };

  // Remove symbol from config
  const handleRemoveSymbol = async (symbol: string, tradingType: string) => {
    setAddingSymbol(symbol); // Reuse same loading state
    try {
      const cfg = await fetchScannerConfig();
      const modeConfig = cfg[tradingType] || { enabled: true, symbols: [], timeframe: "M5" };
      const updatedSymbols = modeConfig.symbols.filter((s: string) => s !== symbol);
      
      await patchScannerConfig({
        [tradingType]: {
          ...modeConfig,
          symbols: updatedSymbols,
        },
      });
      
      await loadActiveSymbols(); // Reload active symbols
      pushNotification({
        type: "success",
        title: "Symbol Removed",
        message: `${symbol} removed from ${TYPE_LABELS[tradingType]} (${updatedSymbols.length}/${MODE_LIMITS[tradingType]})`,
      });
    } catch (err) {
      console.error("Failed to remove symbol:", err);
      pushNotification({
        type: "error",
        title: "Failed to Remove Symbol",
        message: `Could not remove ${symbol} from ${TYPE_LABELS[tradingType]}`,
      });
    } finally {
      setAddingSymbol(null);
    }
  };

  // Get results for active tab
  const results = scanData?.trading_types?.[activeTab] ?? [];

  // Filter and sort
  const filteredResults = results
    .filter((r) =>
      r.symbol.toLowerCase().includes(searchTerm.toLowerCase()) ||
      r.category.toLowerCase().includes(searchTerm.toLowerCase())
    )
    .sort((a, b) => {
      const aVal = a[sortKey];
      const bVal = b[sortKey];
      if (typeof aVal === "number" && typeof bVal === "number") {
        return sortDir === "asc" ? aVal - bVal : bVal - aVal;
      }
      if (typeof aVal === "string" && typeof bVal === "string") {
        return sortDir === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      return 0;
    });

  // Toggle sort
  const handleSort = (key: keyof ScanResult) => {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  return (
    <div className="p-4 sm:p-6 lg:p-8 w-full">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <h1 className="text-3xl font-bold">Market Scanner</h1>
          <div className="flex items-center gap-3">
            {health && (
              <div className="text-xs text-gray-500">
                {health.cache_valid ? "✓ Cache valid" : "⚠ Cache stale"} •{" "}
                {health.mt5_connected ? "✓ MT5" : "❌ MT5"} •{" "}
                Last: {health.last_scan ? relTime(health.last_scan) : "never"}
              </div>
            )}
            <button
              onClick={() => loadData(false)}
              disabled={scanning || loading}
              className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition-colors"
            >
              {scanning ? "Scanning..." : "🔄 Refresh"}
            </button>
            <button
              onClick={() => handleScan()}
              disabled={scanning || loading}
              className="px-3 py-1.5 bg-green-600 hover:bg-green-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition-colors"
            >
              {scanning ? "⏳ Scanning..." : "🔍 Full Scan"}
            </button>
          </div>
        </div>
        <p className="text-gray-400 text-sm">
          Discover the best trading opportunities from 150+ symbols across all markets
        </p>
      </div>

      {/* Stats */}
      {scanData && (
        <div className="grid grid-cols-4 gap-4 mb-6">
          <StatCard label="Total Scanned" value={String(scanData.total_scanned)} />
          <StatCard label="Opportunities Found" value={String(scanData.total_passed)} />
          <StatCard label="Scan Duration" value={`${scanData.scan_duration_seconds.toFixed(1)}s`} />
          <StatCard
            label="Last Scan"
            value={relTime(scanData.timestamp)}
            sub={new Date(scanData.timestamp).toLocaleString()}
          />
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-2 mb-4 border-b border-gray-800">
        <button
          onClick={() => setActiveTab("monitor")}
          className={`px-4 py-2 font-medium transition-colors relative ${
            activeTab === "monitor"
              ? "text-blue-400 border-b-2 border-blue-400"
              : "text-gray-400 hover:text-gray-300"
          }`}
        >
          📡 Monitor
        </button>
        <button
          onClick={() => setActiveTab("analysis")}
          className={`px-4 py-2 font-medium transition-colors relative ${
            activeTab === "analysis"
              ? "text-blue-400 border-b-2 border-blue-400"
              : "text-gray-400 hover:text-gray-300"
          }`}
        >
          🌐 Market Analysis
        </button>
        {TRADING_TYPES.map((type) => {
          const count = scanData?.trading_types?.[type]?.length ?? 0;
          return (
            <button
              key={type}
              onClick={() => setActiveTab(type)}
              className={`px-4 py-2 font-medium transition-colors relative ${
                activeTab === type
                  ? "text-blue-400 border-b-2 border-blue-400"
                  : "text-gray-400 hover:text-gray-300"
              }`}
            >
              {TYPE_LABELS[type]}
              <span className="ml-2 text-xs bg-gray-800 px-1.5 py-0.5 rounded">
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {/* Monitor Tab */}
      {activeTab === "monitor" && (
        <ScannerMonitor />
      )}

      {/* Market Analysis Tab */}
      {activeTab === "analysis" && (
        <MarketAnalysisTab scanData={scanData} />
      )}

      {/* Search — only for scan result tabs */}
      {activeTab !== "monitor" && activeTab !== "analysis" && <div className="mb-4">
        <input
          type="text"
          placeholder="Search symbols or categories..."
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          className="w-full px-4 py-2 bg-gray-900 border border-gray-800 rounded-lg text-gray-100 placeholder-gray-500 focus:outline-none focus:border-blue-500 transition-colors"
        />
      </div>}

      {/* Results Table — only for scan result tabs */}
      {activeTab !== "monitor" && activeTab !== "analysis" && (
        loading ? (
        <div className="text-center py-20 text-gray-500">
          <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500 mb-4"></div>
          <p>Loading scan results...</p>
        </div>
      ) : filteredResults.length === 0 ? (
        <div className="text-center py-20 text-gray-500">
          <p className="text-3xl mb-2">📊</p>
          <p>No symbols found. Try running a full scan.</p>
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-800/50 text-gray-400 text-xs uppercase tracking-wide">
                  <SortableHeader label="Rank" sortKey={null} currentKey={sortKey} sortDir={sortDir} onSort={() => {}} />
                  <SortableHeader label="Symbol" sortKey="symbol" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Score" sortKey="composite_score" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="ATR" sortKey="atr_pips" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Spread" sortKey="spread_pips" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="ADX" sortKey="adx" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Volatility" sortKey="volatility_score" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Trend" sortKey="trend_score" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Liquidity" sortKey="liquidity_subscore" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Category" sortKey="category" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <SortableHeader label="Status" sortKey="trading_hours_active" currentKey={sortKey} sortDir={sortDir} onSort={handleSort} />
                  <th className="text-right py-3 px-4">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredResults.map((result, idx) => (
                  <tr
                    key={result.symbol}
                    className="border-t border-gray-800 hover:bg-gray-800/30 transition-colors"
                  >
                    <td className="py-3 px-4 text-gray-500 font-mono text-xs">
                      #{idx + 1}
                    </td>
                    <td className="py-3 px-4 font-mono font-bold text-gray-100">
                      {result.symbol}
                    </td>
                    <td className="py-3 px-4">
                      <div className="flex items-center gap-2">
                        <div className={`px-2 py-1 rounded border ${scoreBg(result.composite_score)}`}>
                          <span className={`font-mono font-bold ${scoreColor(result.composite_score)}`}>
                            {result.composite_score.toFixed(1)}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td className="py-3 px-4 font-mono text-gray-300 text-xs">
                      {result.atr_pips.toFixed(2)}
                    </td>
                    <td className="py-3 px-4 font-mono text-gray-300 text-xs">
                      {result.spread_pips.toFixed(2)}
                    </td>
                    <td className="py-3 px-4 font-mono text-gray-300 text-xs">
                      {result.adx.toFixed(1)}
                    </td>
                    <td className="py-3 px-4">
                      <ScorePill score={result.volatility_score} />
                    </td>
                    <td className="py-3 px-4">
                      <ScorePill score={result.trend_score} />
                    </td>
                    <td className="py-3 px-4">
                      <ScorePill score={result.liquidity_subscore} />
                    </td>
                    <td className="py-3 px-4">
                      <span className="px-2 py-1 bg-gray-800 text-gray-300 rounded text-xs font-medium">
                        {result.category}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-center">
                      {result.trading_hours_active ? (
                        <span className="text-green-400 text-xs">● Active</span>
                      ) : (
                        <span className="text-gray-500 text-xs">○ Closed</span>
                      )}
                    </td>
                    <td className="py-3 px-4 text-right">
                      {(() => {
                        const isAdded = activeSymbols[activeTab]?.includes(result.symbol);
                        const currentCount = activeSymbols[activeTab]?.length || 0;
                        const limit = MODE_LIMITS[activeTab];
                        const atCapacity = currentCount >= limit;
                        const isProcessing = addingSymbol === result.symbol;

                        if (isAdded) {
                          return (
                            <button
                              onClick={() => handleRemoveSymbol(result.symbol, activeTab)}
                              disabled={isProcessing}
                              className="px-3 py-1 bg-red-600 hover:bg-red-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded text-xs font-medium transition-colors"
                            >
                              {isProcessing ? "Removing..." : "Remove"}
                            </button>
                          );
                        } else if (atCapacity) {
                          return (
                            <button
                              disabled
                              className="px-3 py-1 bg-gray-700 cursor-not-allowed rounded text-xs font-medium text-gray-500"
                              title={`${TYPE_LABELS[activeTab]} scanner is full (${limit}/${limit})`}
                            >
                              Full ({currentCount}/{limit})
                            </button>
                          );
                        } else {
                          return (
                            <button
                              onClick={() => handleAddSymbol(result.symbol, activeTab)}
                              disabled={isProcessing}
                              className="px-3 py-1 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded text-xs font-medium transition-colors"
                            >
                              {isProcessing ? "Adding..." : `+ Add (${currentCount}/${limit})`}
                            </button>
                          );
                        }
                      })()}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )
      )}

      {/* Footer Info */}
      {activeTab !== "monitor" && activeTab !== "analysis" && filteredResults.length > 0 && (
        <div className="mt-4 text-xs text-gray-500 text-center">
          Showing {filteredResults.length} of {results.length} symbols for {TYPE_LABELS[activeTab]}
        </div>
      )}
    </div>
  );
}

// ── sub-components ─────────────────────────────────────────────────────────
function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">{label}</div>
      <div className="text-2xl font-bold font-mono text-gray-100">{value}</div>
      {sub && <div className="text-xs text-gray-500 mt-1">{sub}</div>}
    </div>
  );
}

function SortableHeader({
  label,
  sortKey,
  currentKey,
  sortDir,
  onSort,
}: {
  label: string;
  sortKey: keyof ScanResult | null;
  currentKey: keyof ScanResult;
  sortDir: "asc" | "desc";
  onSort: (key: keyof ScanResult) => void;
}) {
  if (!sortKey) {
    return <th className="text-left py-3 px-4">{label}</th>;
  }

  const isActive = currentKey === sortKey;

  return (
    <th
      className="text-left py-3 px-4 cursor-pointer hover:text-gray-200 transition-colors select-none"
      onClick={() => onSort(sortKey)}
    >
      <div className="flex items-center gap-1">
        {label}
        {isActive && (
          <span className="text-blue-400">
            {sortDir === "asc" ? "↑" : "↓"}
          </span>
        )}
      </div>
    </th>
  );
}

function ScorePill({ score }: { score: number }) {
  return (
    <div className={`inline-block px-2 py-0.5 rounded text-xs font-mono font-medium ${scoreColor(score)}`}>
      {score.toFixed(0)}
    </div>
  );
}
