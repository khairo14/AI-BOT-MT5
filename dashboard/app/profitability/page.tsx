"use client";

import { useEffect, useState } from "react";
import { fetchProfitabilityReport, type ProfitabilityReport } from "@/lib/api";

export default function ProfitabilityPage() {
  const [report, setReport] = useState<ProfitabilityReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedPeriod, setSelectedPeriod] = useState<number | null>(null);
  const [selectedAccount, setSelectedAccount] = useState<"all" | "paper" | "live">("all");
  const [exporting, setExporting] = useState(false);

  const loadReport = async (days?: number) => {
    setLoading(true);
    try {
      const data = await fetchProfitabilityReport(days);
      setReport(data);
    } catch (err) {
      console.error("Failed to load profitability report:", err);
    } finally {
      setLoading(false);
    }
  };

  const handleExport = async (format: "csv" | "excel") => {
    setExporting(true);
    try {
      const params = new URLSearchParams({
        account: selectedAccount,
        format: format,
      });
      if (selectedPeriod) {
        params.append("days", selectedPeriod.toString());
      }

      const response = await fetch(`http://localhost:8000/trades/journal/export?${params.toString()}`);
      
      if (!response.ok) {
        throw new Error(`Export failed: ${response.statusText}`);
      }

      // Get filename from Content-Disposition header or generate default
      const contentDisposition = response.headers.get("Content-Disposition");
      const filenameMatch = contentDisposition?.match(/filename=(.+)/);
      const filename = filenameMatch
        ? filenameMatch[1].replace(/"/g, "")
        : `trades_${format === "csv" ? "export.csv" : "export.xlsx"}`;

      // Download file
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(url);
      document.body.removeChild(a);
    } catch (err) {
      console.error("Export failed:", err);
      alert("Failed to export trades. Please try again.");
    } finally {
      setExporting(false);
    }
  };

  useEffect(() => {
    loadReport(selectedPeriod || undefined);
  }, [selectedPeriod]);

  if (loading) {
    return (
      <div className="min-h-screen bg-gray-950 text-gray-100 p-6 flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-b-2 border-blue-500 mx-auto mb-4"></div>
          <p className="text-gray-400">Loading profitability report...</p>
        </div>
      </div>
    );
  }

  if (!report) {
    return (
      <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
        <div className="text-center py-20">
          <p className="text-gray-400">No trade data available yet.</p>
          <p className="text-sm text-gray-500 mt-2">Start trading to generate a profitability report.</p>
        </div>
      </div>
    );
  }

  const { overall, success_criteria, task_2_status, by_trading_type, by_symbol } = report;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      {/* Header */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <h1 className="text-3xl font-bold">� Profitability Report</h1>
          <div className="flex items-center gap-3">
            <select
              value={selectedPeriod || "all"}
              onChange={(e) => setSelectedPeriod(e.target.value === "all" ? null : Number(e.target.value))}
              className="px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm"
            >
              <option value="all">All Time</option>
              <option value="7">Last 7 Days</option>
              <option value="14">Last 14 Days</option>
              <option value="30">Last 30 Days</option>
            </select>
            <select
              value={selectedAccount}
              onChange={(e) => setSelectedAccount(e.target.value as "all" | "paper" | "live")}
              className="px-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm"
            >
              <option value="all">All Accounts</option>
              <option value="paper">Paper Only</option>
              <option value="live">Live Only</option>
            </select>
            <div className="h-6 border-l border-gray-700"></div>
            <button              onClick={() => handleExport("csv")}
              disabled={exporting}
              className="px-3 py-1.5 bg-green-600 hover:bg-green-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
            >
              {exporting ? "⏳" : "📄"} CSV
            </button>
            <button
              onClick={() => handleExport("excel")}
              disabled={exporting}
              className="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:bg-gray-700 disabled:cursor-not-allowed rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
            >
              {exporting ? "⏳" : "📊"} Excel
            </button>
            <button              onClick={() => loadReport(selectedPeriod || undefined)}
              className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium transition-colors"
            >
              🔄 Refresh
            </button>
          </div>
        </div>
        <p className="text-gray-400 text-sm">
          Monitor system performance: win rate, profit factor, and profitability across all strategies
        </p>
      </div>

      {/* Task Status Banner */}
      <div
        className={`mb-6 p-4 rounded-xl border ${
          task_2_status.status === "PASSED"
            ? "bg-green-500/10 border-green-500/30"
            : "bg-yellow-500/10 border-yellow-500/30"
        }`}
      >
        <div className="flex items-center gap-3">
          <div className="text-3xl">{task_2_status.status === "PASSED" ? "✅" : "⏳"}</div>
          <div className="flex-1">
            <h2 className="text-lg font-bold mb-1">{task_2_status.message}</h2>
            <p className="text-sm text-gray-400">{task_2_status.recommendation}</p>
          </div>
        </div>
      </div>

      {/* Success Criteria Grid */}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
        <CriteriaCard
          label="50+ Trades"
          value={`${overall.total_trades}/50`}
          met={success_criteria["50_trades"]}
          progress={(overall.total_trades / 50) * 100}
        />
        <CriteriaCard
          label="Win Rate ≥50%"
          value={`${overall.win_rate}%`}
          met={success_criteria.win_rate_50}
          progress={Math.min((overall.win_rate / 50) * 100, 100)}
        />
        <CriteriaCard
          label="Win Rate ≥55% (Ideal)"
          value={`${overall.win_rate}%`}
          met={success_criteria.win_rate_55}
          progress={Math.min((overall.win_rate / 55) * 100, 100)}
        />
        <CriteriaCard
          label="Profit Factor ≥1.5"
          value={overall.profit_factor.toFixed(2)}
          met={success_criteria.profit_factor_1_5}
          progress={Math.min((overall.profit_factor / 1.5) * 100, 100)}
        />
      </div>

      {/* Overall Metrics */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6">
        <h2 className="text-xl font-bold mb-4">Overall Performance</h2>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <MetricCard label="Total Trades" value={overall.total_trades} />
          <MetricCard
            label="Win/Loss"
            value={`${overall.wins}W / ${overall.losses}L / ${overall.breakeven}BE`}
          />
          <MetricCard
            label="Total Profit"
            value={`$${overall.total_profit.toFixed(2)}`}
            color={overall.total_profit >= 0 ? "text-green-400" : "text-red-400"}
          />
          <MetricCard label="Avg Win" value={`$${overall.avg_profit.toFixed(2)}`} />
          <MetricCard label="Avg Loss" value={`$${overall.avg_loss.toFixed(2)}`} />
        </div>
      </div>

      {/* By Trading Type */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 mb-6">
        <h2 className="text-xl font-bold mb-4">By Trading Type</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {Object.entries(by_trading_type).map(([type, metrics]) => (
            <div key={type} className="bg-gray-800/50 border border-gray-700 rounded-lg p-4">
              <h3 className="font-bold text-lg mb-3 capitalize">{type.replace("_", " ")}</h3>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-400">Trades:</span>
                  <span className="font-mono">{metrics.total_trades}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Win Rate:</span>
                  <span className={`font-mono ${metrics.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>
                    {metrics.win_rate}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Profit Factor:</span>
                  <span className={`font-mono ${metrics.profit_factor >= 1.5 ? "text-green-400" : "text-yellow-400"}`}>
                    {metrics.profit_factor.toFixed(2)}
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-400">Total Profit:</span>
                  <span className={`font-mono ${metrics.total_profit >= 0 ? "text-green-400" : "text-red-400"}`}>
                    ${metrics.total_profit.toFixed(2)}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Top Symbols */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h2 className="text-xl font-bold mb-4">Symbol Performance (Top 10 by Profit)</h2>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-800/50 text-gray-400 text-xs uppercase tracking-wide">
                <th className="text-left py-3 px-4">Symbol</th>
                <th className="text-right py-3 px-4">Trades</th>
                <th className="text-right py-3 px-4">Win Rate</th>
                <th className="text-right py-3 px-4">Profit Factor</th>
                <th className="text-right py-3 px-4">Total Profit</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(by_symbol)
                .sort(([, a], [, b]) => b.total_profit - a.total_profit)
                .slice(0, 10)
                .map(([symbol, metrics]) => (
                  <tr key={symbol} className="border-b border-gray-800/50 hover:bg-gray-800/30">
                    <td className="py-3 px-4 font-mono font-bold">{symbol}</td>
                    <td className="py-3 px-4 text-right font-mono">{metrics.total_trades}</td>
                    <td className="py-3 px-4 text-right">
                      <span className={`font-mono ${metrics.win_rate >= 50 ? "text-green-400" : "text-red-400"}`}>
                        {metrics.win_rate}%
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right">
                      <span className={`font-mono ${metrics.profit_factor >= 1.5 ? "text-green-400" : "text-yellow-400"}`}>
                        {metrics.profit_factor.toFixed(2)}
                      </span>
                    </td>
                    <td className="py-3 px-4 text-right">
                      <span className={`font-mono ${metrics.total_profit >= 0 ? "text-green-400" : "text-red-400"}`}>
                        ${metrics.total_profit.toFixed(2)}
                      </span>
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// Helper Components
function CriteriaCard({
  label,
  value,
  met,
  progress,
}: {
  label: string;
  value: string;
  met: boolean;
  progress: number;
}) {
  return (
    <div className={`bg-gray-900 border rounded-xl p-4 ${met ? "border-green-500/30" : "border-gray-800"}`}>
      <div className="flex items-center gap-2 mb-2">
        <div className="text-xl">{met ? "✅" : "⏳"}</div>
        <div className="text-xs text-gray-400 uppercase">{label}</div>
      </div>
      <div className="text-2xl font-bold mb-2">{value}</div>
      <div className="w-full bg-gray-800 rounded-full h-2 overflow-hidden">
        <div
          className={`h-full transition-all ${met ? "bg-green-500" : "bg-yellow-500"}`}
          style={{ width: `${Math.min(progress, 100)}%` }}
        ></div>
      </div>
    </div>
  );
}

function MetricCard({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="bg-gray-800/50 border border-gray-700 rounded-lg p-3">
      <div className="text-xs text-gray-400 uppercase mb-1">{label}</div>
      <div className={`text-lg font-bold ${color || "text-gray-100"}`}>{value}</div>
    </div>
  );
}
