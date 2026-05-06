"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import { fetchPortfolioStatus, fetchPortfolioOptimalAllocation, togglePortfolioOptimization, fetchAccounts } from "@/lib/api";
import type { PortfolioStatus, PortfolioOptimalAllocation } from "@/types";

function pct(n: number | undefined): string {
  if (n == null || isNaN(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}

function ToggleSwitch({ enabled, onChange, disabled }: { enabled: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      onClick={() => !disabled && onChange(!enabled)}
      disabled={disabled}
      className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
        enabled ? "bg-green-600" : "bg-gray-700"
      } ${disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}`}
    >
      <span
        className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
          enabled ? "translate-x-6" : "translate-x-1"
        }`}
      />
    </button>
  );
}

function CorrelationMatrix({ matrix }: { matrix: Record<string, Record<string, number>> }) {
  const strategies = Object.keys(matrix);
  if (strategies.length === 0) return <div className="text-gray-500 text-sm">No data</div>;

  const getColor = (val: number) => {
    if (val > 0.7) return "bg-red-500/20 text-red-300";
    if (val > 0.3) return "bg-yellow-500/20 text-yellow-300";
    if (val > -0.3) return "bg-blue-500/20 text-blue-300";
    return "bg-green-500/20 text-green-300";
  };

  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead>
          <tr>
            <th className="p-2 text-left text-gray-500"></th>
            {strategies.map((s) => (
              <th key={s} className="p-2 text-center text-gray-500 font-mono">{s.substring(0, 8)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {strategies.map((row) => (
            <tr key={row}>
              <td className="p-2 text-gray-400 font-mono">{row.substring(0, 8)}</td>
              {strategies.map((col) => {
                const val = matrix[row]?.[col] ?? 0;
                return (
                  <td key={col} className={`p-2 text-center font-mono ${getColor(val)}`}>
                    {val.toFixed(2)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function PortfolioPage() {
  const [status, setStatus] = useState<PortfolioStatus | null>(null);
  const [allocation, setAllocation] = useState<PortfolioOptimalAllocation | null>(null);
  const [selectedAccountLogin, setSelectedAccountLogin] = useState<number | null>(null);
  const [accounts, setAccounts] = useState<Array<{ login: number; type: string }>>([]);
  const [loading, setLoading] = useState(true);
  const [toggling, setToggling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const debounceTimerRef = useRef<NodeJS.Timeout | undefined>(undefined); // Fixed line

  // Load available accounts
  useEffect(() => {
    fetchAccounts()
      .then(res => setAccounts(res.accounts))
      .catch(() => {});
  }, []);

  const load = useCallback(async (accountLogin?: number | null) => {
    try {
      setLoading(true);
      setError(null);
      // Reset previous data while loading new account data
      setAllocation(null);
      setStatus(null);
      
      const [s, a] = await Promise.all([
        fetchPortfolioStatus(),
        fetchPortfolioOptimalAllocation(accountLogin ?? undefined).catch(() => null),
      ]);
      setStatus(s);
      setAllocation(a);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load portfolio data");
    } finally {
      setLoading(false);
    }
  }, []);

  // Debounced account switching
  const handleAccountChange = useCallback((login: number | null) => {
    setSelectedAccountLogin(login);
    
    // Clear existing timer
    if (debounceTimerRef.current) {
      clearTimeout(debounceTimerRef.current);
    }
    
    // Debounce the API call
    debounceTimerRef.current = setTimeout(() => {
      load(login);
    }, 300);
  }, [load]);

  // Initial load
  useEffect(() => {
    load(selectedAccountLogin);
    
    // Cleanup timer on unmount
    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, []); // Empty dependency array - only run once on mount

  const handleToggle = async (enabled: boolean) => {
    try {
      setToggling(true);
      await togglePortfolioOptimization(enabled);
      await load(selectedAccountLogin);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to toggle optimization");
    } finally {
      setToggling(false);
    }
  };

  if (loading) {
    return (
      <div className="p-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-800 rounded w-64"></div>
          <div className="h-32 bg-gray-800 rounded"></div>
        </div>
      </div>
    );
  }

  const strategies = allocation?.by_strategy ? Object.keys(allocation.by_strategy) : [];
  const needsRebalance = allocation?.needs_rebalance ?? false;

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h2 className="text-2xl font-bold text-white">Portfolio Optimization</h2>
          <p className="text-gray-500 text-sm mt-1">
            Kelly Criterion + Risk Parity allocation across strategies
          </p>
        </div>
        <div className="flex items-center gap-4 flex-wrap">
          {/* Account login filter */}
          {accounts.length > 0 && (
            <select
              value={selectedAccountLogin ?? ""}
              onChange={(e) => handleAccountChange(e.target.value ? Number(e.target.value) : null)}
              className="px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm"
            >
              <option value="">All Accounts</option>
              {accounts.map((acc) => (
                <option key={acc.login} value={acc.login}>
                  {acc.login} ({acc.type === "demo" ? "Demo" : "Live"})
                </option>
              ))}
            </select>
          )}
          {status && (
            <div className="flex items-center gap-3">
              <span className="text-sm text-gray-400">
                {status.enabled ? "Enabled" : "Disabled"}
              </span>
              <ToggleSwitch
                enabled={status.enabled}
                onChange={handleToggle}
                disabled={toggling}
              />
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="bg-red-900/20 border border-red-500/30 rounded-xl p-4 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Status Card */}
      {status && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
            Configuration
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <span className="text-xs text-gray-500">Status</span>
              <div className={`text-lg font-bold ${status.enabled ? "text-green-400" : "text-gray-500"}`}>
                {status.enabled ? "Active" : "Inactive"}
              </div>
            </div>
            <div>
              <span className="text-xs text-gray-500">Min Trades Required</span>
              <div className="text-lg font-bold text-white font-mono">{status.min_trades_required}</div>
            </div>
            <div>
              <span className="text-xs text-gray-500">Rebalance Threshold</span>
              <div className="text-lg font-bold text-white font-mono">
                {pct(status.rebalance_threshold)}
              </div>
            </div>
          </div>
          {status.message && (
            <div className="mt-4 text-sm text-gray-400 italic">{status.message}</div>
          )}
        </div>
      )}

      {/* Allocation Table */}
      {allocation && allocation.enabled && strategies.length > 0 && (
        <>
          <div className={`border rounded-xl p-6 ${
            needsRebalance 
              ? "bg-yellow-900/10 border-yellow-500/30" 
              : "bg-gray-900 border-gray-800"
          }`}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
                Optimal Allocations
              </h3>
              {needsRebalance && (
                <span className="px-3 py-1 bg-yellow-500/20 text-yellow-300 border border-yellow-500/30 rounded-full text-xs font-semibold">
                  Rebalance Recommended
                </span>
              )}
            </div>
            
            <div className="text-xs text-gray-500 mb-4">
              Total trades: <span className="font-mono text-white">{allocation.total_trades}</span>
            </div>

            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-800">
                    <th className="text-left p-3 text-gray-500 font-semibold">Strategy</th>
                    <th className="text-right p-3 text-gray-500 font-semibold">Kelly</th>
                    <th className="text-right p-3 text-gray-500 font-semibold">Risk Parity</th>
                    <th className="text-right p-3 text-gray-500 font-semibold">Current</th>
                    <th className="text-right p-3 text-gray-500 font-semibold">Recommended</th>
                    <th className="text-right p-3 text-gray-500 font-semibold">Deviation</th>
                    <th className="text-center p-3 text-gray-500 font-semibold">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {strategies.map((strategy) => {
                    const s = allocation.by_strategy[strategy];
                    const deviationColor = Math.abs(s.deviation) > allocation.rebalance_threshold
                      ? "text-yellow-400"
                      : "text-green-400";
                    return (
                      <tr key={strategy} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                        <td className="p-3 text-white font-mono">{strategy}</td>
                        <td className="p-3 text-right font-mono text-gray-300">{pct(s.kelly_allocation)}</td>
                        <td className="p-3 text-right font-mono text-gray-300">{pct(s.risk_parity_allocation)}</td>
                        <td className="p-3 text-right font-mono text-gray-300">{pct(s.current_allocation)}</td>
                        <td className="p-3 text-right font-mono text-white font-bold">{pct(s.recommended_allocation)}</td>
                        <td className={`p-3 text-right font-mono ${deviationColor}`}>
                          {s.deviation >= 0 ? "+" : ""}{pct(s.deviation)}
                        </td>
                        <td className="p-3 text-center">
                          {s.needs_rebalance ? (
                            <span className="text-yellow-400 text-xs">⚠️ Rebalance</span>
                          ) : (
                            <span className="text-green-400 text-xs">✓ OK</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>

          {/* Correlation Matrix */}
          {allocation.correlation_matrix && Object.keys(allocation.correlation_matrix).length > 0 && (
            <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
              <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
                Strategy Correlation Matrix
              </h3>
              <p className="text-xs text-gray-500 mb-4">
                High correlation (red) means strategies move together. Low/negative correlation (green) provides diversification.
              </p>
              <CorrelationMatrix matrix={allocation.correlation_matrix} />
            </div>
          )}
        </>
      )}

      {/* Empty State */}
      {allocation && !allocation.enabled && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-12 text-center">
          <div className="text-gray-500 text-lg mb-2">Portfolio Optimization Disabled</div>
          <p className="text-gray-600 text-sm max-w-md mx-auto">
            {allocation.message || "Enable optimization above to see Kelly Criterion and Risk Parity allocations."}
          </p>
        </div>
      )}

      {allocation && allocation.enabled && strategies.length === 0 && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-12 text-center">
          <div className="text-gray-500 text-lg mb-2">Insufficient Data</div>
          <p className="text-gray-600 text-sm max-w-md mx-auto">
            {allocation.message || `Need at least ${allocation.min_trades_required} trades per strategy to calculate optimal allocations.`}
          </p>
        </div>
      )}
    </div>
  );
}