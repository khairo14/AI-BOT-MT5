"use client";

import { useState, useEffect } from "react";
import { fetchScannerPerformance } from "@/lib/api";
import { useBotStore } from "@/lib/store";

type AccountFilter = "paper" | "live" | "all";

interface PairStat {
  symbol: string;
  signals: number;
  trades: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_profit: number;
  last_signal: string | null;
  status: string;
}

interface TypeStats {
  enabled: boolean;
  total_active: number;
  total_signals: number;
  total_trades: number;
  total_profit: number;
  avg_win_rate: number;
  active_pairs: PairStat[];
}

const MODE_CONFIG = {
  scalping: { label: "Scalping", icon: "⚡", color: "yellow", maxSlots: 7 },
  day_trading: { label: "Day Trading", icon: "☀️", color: "blue", maxSlots: 15 },
  swing: { label: "Swing", icon: "〰️", color: "purple", maxSlots: 18 },
};

function normalizeAccountFilter(mode?: string | null): AccountFilter {
  const normalized = String(mode || "").toLowerCase();

  if (normalized === "live") return "live";
  if (normalized === "demo" || normalized === "paper") return "paper";

  return "paper";
}

function accountFilterLabel(filter: AccountFilter) {
  if (filter === "paper") return "Demo";
  if (filter === "live") return "Live";
  return "All";
}

export default function ScannerMonitor() {
  const currentMode = useBotStore((s) =>
    normalizeAccountFilter(s.account?.account_type || s.account?.mode)
  );

  const [accountFilter, setAccountFilter] =
    useState<AccountFilter>(currentMode);

  const [data, setData] = useState<Record<string, TypeStats> | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdate, setLastUpdate] = useState<string>("");

  const loadData = async () => {
    try {
      const result = await fetchScannerPerformance(accountFilter);
      setData(result.trading_types);
      setLastUpdate(new Date(result.timestamp).toLocaleTimeString());
    } catch (err) {
      console.error("Failed to load scanner performance:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setAccountFilter(currentMode);
  }, [currentMode]);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 60000);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountFilter]);

  if (loading) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 animate-pulse">
        <div className="h-6 bg-gray-800 rounded w-1/3 mb-4"></div>
        <div className="space-y-3">
          <div className="h-16 bg-gray-800 rounded"></div>
          <div className="h-16 bg-gray-800 rounded"></div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <p className="text-gray-500 text-sm">
          Failed to load scanner performance data
        </p>
      </div>
    );
  }

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Scanner Monitor
          </h3>
          <p className="text-xs text-gray-500 mt-1">
            Live active pairs · Last update: {lastUpdate}
          </p>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex gap-1 bg-gray-800 rounded-lg p-0.5">
            {(["paper", "live", "all"] as const).map((opt) => (
              <button
                key={opt}
                onClick={() => setAccountFilter(opt)}
                className={`px-2.5 py-1 rounded text-xs font-medium transition-colors ${
                  accountFilter === opt
                    ? opt === "live"
                      ? "bg-red-600 text-white"
                      : opt === "paper"
                        ? "bg-emerald-600 text-white"
                        : "bg-blue-600 text-white"
                    : "text-gray-400 hover:text-white"
                }`}
              >
                {accountFilterLabel(opt)}
              </button>
            ))}
          </div>

          <button
            onClick={loadData}
            className="text-xs text-gray-500 hover:text-white transition-colors"
          >
            ↻ Refresh
          </button>
        </div>
      </div>

      <div className="space-y-4">
        {(
          Object.entries(MODE_CONFIG) as [
            keyof typeof MODE_CONFIG,
            (typeof MODE_CONFIG)[keyof typeof MODE_CONFIG],
          ][]
        ).map(([modeKey, config]) => {
          const stats = data[modeKey];
          if (!stats) return null;

          const { enabled, active_pairs } = stats;
          const slotsUsed = active_pairs.length;
          const slotsAvailable = config.maxSlots - slotsUsed;

          return (
            <div
              key={modeKey}
              className={`border ${
                enabled ? "border-gray-700" : "border-gray-800/50"
              } rounded-lg p-4 ${
                enabled ? "bg-gray-800/30" : "bg-gray-900/50"
              }`}
            >
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <span className="text-lg">{config.icon}</span>
                  <h4 className="font-medium text-white">{config.label}</h4>
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                      enabled
                        ? "bg-emerald-900/50 text-emerald-400"
                        : "bg-gray-700/50 text-gray-500"
                    }`}
                  >
                    {enabled ? "Active" : "Paused"}
                  </span>
                </div>

                <div className="text-xs text-gray-500">
                  {slotsUsed}/{config.maxSlots} slots
                  {slotsAvailable > 0 && (
                    <span className="text-gray-600 ml-1">
                      ({slotsAvailable} free)
                    </span>
                  )}
                </div>
              </div>

              {slotsUsed > 0 && (
                <div className="grid grid-cols-4 gap-3 mb-3">
                  <div className="bg-gray-900/50 rounded px-2 py-1.5">
                    <div className="text-xs text-gray-500">Trades</div>
                    <div className="text-sm font-mono text-white">
                      {stats.total_trades}
                    </div>
                  </div>

                  <div className="bg-gray-900/50 rounded px-2 py-1.5">
                    <div className="text-xs text-gray-500">Win Rate</div>
                    <div
                      className={`text-sm font-mono ${
                        stats.avg_win_rate >= 50
                          ? "text-green-400"
                          : "text-red-400"
                      }`}
                    >
                      {stats.avg_win_rate.toFixed(1)}%
                    </div>
                  </div>

                  <div className="bg-gray-900/50 rounded px-2 py-1.5 col-span-2">
                    <div className="text-xs text-gray-500">Profit</div>
                    <div
                      className={`text-sm font-mono ${
                        stats.total_profit >= 0
                          ? "text-green-400"
                          : "text-red-400"
                      }`}
                    >
                      {stats.total_profit >= 0 ? "+" : ""}$
                      {stats.total_profit.toFixed(2)}
                    </div>
                  </div>
                </div>
              )}

              {slotsUsed === 0 ? (
                <div className="text-xs text-gray-600 text-center py-2 border border-dashed border-gray-700 rounded">
                  No active pairs · Add from Market Scanner
                </div>
              ) : (
                <div className="space-y-2">
                  {active_pairs.map((pair) => (
                    <div
                      key={pair.symbol}
                      className="flex items-center justify-between bg-gray-900/50 rounded px-3 py-2 text-xs"
                    >
                      <div className="flex items-center gap-3 flex-1">
                        <span className="font-mono font-medium text-white w-16">
                          {pair.symbol}
                        </span>

                        <div className="flex items-center gap-3 text-gray-500">
                          <span>{pair.trades} trades</span>

                          {pair.trades > 0 && (
                            <>
                              <span
                                className={`font-mono ${
                                  pair.win_rate >= 50
                                    ? "text-green-400"
                                    : "text-red-400"
                                }`}
                              >
                                {pair.win_rate.toFixed(0)}%
                              </span>

                              <span
                                className={`font-mono ${
                                  pair.total_profit >= 0
                                    ? "text-green-400"
                                    : "text-red-400"
                                }`}
                              >
                                {pair.total_profit >= 0 ? "+" : ""}$
                                {pair.total_profit.toFixed(2)}
                              </span>
                            </>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-2">
                        {pair.trades === 0 ? (
                          <span className="text-yellow-600">⏳ Waiting</span>
                        ) : (
                          <span className="text-green-600">✓ Active</span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}