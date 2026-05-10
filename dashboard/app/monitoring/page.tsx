"use client";

import { useCallback, useEffect, useState } from "react";
import { fetchMonitoringMetrics } from "@/lib/api";

interface MonitoringMetrics {
  system: {
    cpu_usage_percent: number;
    memory_usage_percent: number;
    disk_usage_percent: number;
    thread_count: number;
  };
  trading: {
    open_positions: number;
    total_pnl: number;
    trades_today: number;
    signal_queue_size: number;
  };
  mt5: {
    connected: boolean;
  };
  api: {
    total_requests: number;
    total_errors: number;
    avg_latency_ms: number;
    endpoints: Record<
      string,
      {
        requests: number;
        errors: number;
        avg_duration_ms: number;
      }
    >;
  };
}

interface MaintenanceSummary {
  system_status: string;
  severity: "ok" | "info" | "warning" | "error" | "critical";
  feed_ok: boolean;
  feed_stale_count: number;
  market_closed: boolean;
  trade_reconciliation_ok: boolean;
  trade_state_ok: boolean;
  journal_hygiene_ok: boolean;
  learning_valid_rate: number;
  invalid_learning_rows: number;
  unknown_strategy_count: number;
  should_freeze_rl_learning: boolean;
  stale_model_count: number;
  failed_model_count: number;
  runtime_rejection_rate: number;
  pending_signal_count: number;
}

interface MaintenanceHealthResponse {
  success: boolean;
  health?: {
    severity: "ok" | "info" | "warning" | "error" | "critical" | string;
    status: string;
    recommendations: string[];
    dashboard_summary: MaintenanceSummary;
  };
  error?: string;
}

type CardStatus = "good" | "warning" | "critical";

function MetricCard({
  label,
  value,
  unit = "",
  status,
  threshold,
}: {
  label: string;
  value: number | string;
  unit?: string;
  status?: CardStatus;
  threshold?: { warning: number; critical: number };
}) {
  let statusColor = "text-gray-400";
  let bgColor = "bg-gray-900";
  let borderColor = "border-gray-800";

  if (status === "good") {
    statusColor = "text-green-400";
    bgColor = "bg-green-900/10";
    borderColor = "border-green-500/30";
  } else if (status === "warning") {
    statusColor = "text-yellow-400";
    bgColor = "bg-yellow-900/10";
    borderColor = "border-yellow-500/30";
  } else if (status === "critical") {
    statusColor = "text-red-400";
    bgColor = "bg-red-900/10";
    borderColor = "border-red-500/30";
  }

  return (
    <div className={`${bgColor} border ${borderColor} rounded-xl p-4`}>
      <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">
        {label}
      </div>
      <div className={`text-2xl font-bold font-mono ${statusColor}`}>
        {typeof value === "number" ? value.toFixed(1) : value}
        {unit && <span className="text-sm ml-1">{unit}</span>}
      </div>
      {threshold && typeof value === "number" && (
        <div className="text-xs text-gray-600 mt-1">
          Warn: {threshold.warning}
          {unit} / Crit: {threshold.critical}
          {unit}
        </div>
      )}
    </div>
  );
}

function getStatus(value: number, warning: number, critical: number): CardStatus {
  if (value >= critical) return "critical";
  if (value >= warning) return "warning";
  return "good";
}

function statusBadgeClass(severity: string): string {
  if (severity === "critical" || severity === "error") {
    return "bg-red-900/30 text-red-300 border border-red-500/30";
  }

  if (severity === "warning") {
    return "bg-yellow-900/30 text-yellow-300 border border-yellow-500/30";
  }

  if (severity === "info") {
    return "bg-blue-900/30 text-blue-300 border border-blue-500/30";
  }

  return "bg-green-900/30 text-green-300 border border-green-500/30";
}

function ProgressBar({
  value,
  max = 100,
  color = "bg-blue-500",
}: {
  value: number;
  max?: number;
  color?: string;
}) {
  const pct = Math.min((value / max) * 100, 100);

  return (
    <div className="w-full bg-gray-800 rounded-full h-2">
      <div
        className={`${color} h-2 rounded-full transition-all duration-300`}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function MaintenancePanel({
  maintenance,
}: {
  maintenance: MaintenanceHealthResponse | null;
}) {
  if (!maintenance?.success || !maintenance.health?.dashboard_summary) {
    return null;
  }

  const health = maintenance.health;
  const summary = health.dashboard_summary;
  const feedStatus: CardStatus = summary.feed_ok ? "good" : "warning";

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            EVOTRADE Maintenance Health
          </h3>
          <p className="text-xs text-gray-500 mt-1">
            Safe read-only audit summary from the maintenance agent
          </p>
        </div>
        <span
          className={`px-3 py-1 rounded-full text-xs font-bold uppercase w-fit ${statusBadgeClass(
            health.severity
          )}`}
        >
          {health.status}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard
          label="Feed"
          value={
            summary.market_closed ? "Market Closed" : summary.feed_ok ? "OK" : "Stale"
          }
          status={feedStatus}
        />
        <MetricCard
          label="Reconciliation"
          value={summary.trade_reconciliation_ok ? "OK" : "Issue"}
          status={summary.trade_reconciliation_ok ? "good" : "critical"}
        />
        <MetricCard
          label="Trade State"
          value={summary.trade_state_ok ? "OK" : "Issue"}
          status={summary.trade_state_ok ? "good" : "critical"}
        />
        <MetricCard
          label="Learning Valid"
          value={summary.learning_valid_rate * 100}
          unit="%"
          status={summary.should_freeze_rl_learning ? "critical" : "good"}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <MetricCard
          label="Invalid Learning Rows"
          value={summary.invalid_learning_rows}
          status={summary.invalid_learning_rows > 0 ? "warning" : "good"}
        />
        <MetricCard
          label="Unknown Strategy Names"
          value={summary.unknown_strategy_count}
          status={summary.unknown_strategy_count > 0 ? "warning" : "good"}
        />
        <MetricCard
          label="Stale Models"
          value={summary.stale_model_count}
          status={summary.stale_model_count > 0 ? "warning" : "good"}
        />
        <MetricCard
          label="Pending Signals"
          value={summary.pending_signal_count}
          status={summary.pending_signal_count > 10 ? "warning" : "good"}
        />
      </div>

      {health.recommendations.length > 0 && (
        <div className="border-t border-gray-800 pt-4">
          <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">
            Recommendations
          </div>
          <ul className="space-y-1">
            {health.recommendations.slice(0, 4).map((item, idx) => (
              <li key={`${item}-${idx}`} className="text-sm text-gray-300">
                • {item}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

async function fetchMaintenanceHealth(): Promise<MaintenanceHealthResponse | null> {
  const response = await fetch("http://127.0.0.1:8000/maintenance/health", {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`Maintenance health request failed: ${response.status}`);
  }

  return (await response.json()) as MaintenanceHealthResponse;
}

export default function MonitoringPage() {
  const [metrics, setMetrics] = useState<MonitoringMetrics | null>(null);
  const [maintenance, setMaintenance] = useState<MaintenanceHealthResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const load = useCallback(async () => {
    try {
      setError(null);
      const [metricsData, maintenanceData] = await Promise.allSettled([
        fetchMonitoringMetrics(),
        fetchMaintenanceHealth(),
      ]);

      if (metricsData.status === "fulfilled") {
        setMetrics(metricsData.value);
      } else {
        throw metricsData.reason;
      }

      if (maintenanceData.status === "fulfilled") {
        setMaintenance(maintenanceData.value);
      } else {
        setMaintenance(null);
      }

      setLastUpdate(new Date());
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to load monitoring data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const interval = setInterval(load, 5000);
    return () => clearInterval(interval);
  }, [load]);

  if (loading) {
    return (
      <div className="p-8">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-800 rounded w-64" />
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            {[...Array(8)].map((_, i) => (
              <div key={i} className="h-32 bg-gray-800 rounded" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="p-8">
        <div className="bg-red-900/20 border border-red-500/30 rounded-xl p-6 text-red-300">
          <div className="text-lg font-bold mb-2">Error Loading Metrics</div>
          <div className="text-sm">{error}</div>
          <button
            onClick={load}
            className="mt-4 px-4 py-2 bg-red-600 hover:bg-red-700 rounded-lg text-white text-sm font-semibold transition-colors"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!metrics) return null;

  const cpuStatus = getStatus(metrics.system.cpu_usage_percent, 80, 95);
  const memStatus = getStatus(metrics.system.memory_usage_percent, 85, 95);
  const diskStatus = getStatus(metrics.system.disk_usage_percent, 80, 90);

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold text-white">System Monitoring</h2>
          <p className="text-gray-500 text-sm mt-1">
            Real-time metrics from Prometheus exporter and EVOTRADE maintenance agent
          </p>
        </div>
        <div className="text-right">
          <button
            onClick={load}
            className="px-4 py-2 bg-gray-800 hover:bg-gray-700 rounded-lg text-white text-sm font-semibold transition-colors"
          >
            Refresh
          </button>
          {lastUpdate && (
            <div className="text-xs text-gray-500 mt-1">
              Updated {lastUpdate.toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>

      <MaintenancePanel maintenance={maintenance} />

      <div>
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
          System Health
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <MetricCard
            label="CPU Usage"
            value={metrics.system.cpu_usage_percent}
            unit="%"
            status={cpuStatus}
            threshold={{ warning: 80, critical: 95 }}
          />
          <MetricCard
            label="Memory Usage"
            value={metrics.system.memory_usage_percent}
            unit="%"
            status={memStatus}
            threshold={{ warning: 85, critical: 95 }}
          />
          <MetricCard
            label="Disk Usage"
            value={metrics.system.disk_usage_percent}
            unit="%"
            status={diskStatus}
            threshold={{ warning: 80, critical: 90 }}
          />
          <MetricCard
            label="Thread Count"
            value={metrics.system.thread_count}
            status="good"
          />
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6 space-y-4">
        <div>
          <div className="flex justify-between text-sm mb-2">
            <span className="text-gray-400">CPU</span>
            <span
              className={
                cpuStatus === "critical"
                  ? "text-red-400"
                  : cpuStatus === "warning"
                    ? "text-yellow-400"
                    : "text-green-400"
              }
            >
              {metrics.system.cpu_usage_percent.toFixed(1)}%
            </span>
          </div>
          <ProgressBar
            value={metrics.system.cpu_usage_percent}
            color={
              cpuStatus === "critical"
                ? "bg-red-500"
                : cpuStatus === "warning"
                  ? "bg-yellow-500"
                  : "bg-green-500"
            }
          />
        </div>
        <div>
          <div className="flex justify-between text-sm mb-2">
            <span className="text-gray-400">Memory</span>
            <span
              className={
                memStatus === "critical"
                  ? "text-red-400"
                  : memStatus === "warning"
                    ? "text-yellow-400"
                    : "text-green-400"
              }
            >
              {metrics.system.memory_usage_percent.toFixed(1)}%
            </span>
          </div>
          <ProgressBar
            value={metrics.system.memory_usage_percent}
            color={
              memStatus === "critical"
                ? "bg-red-500"
                : memStatus === "warning"
                  ? "bg-yellow-500"
                  : "bg-green-500"
            }
          />
        </div>
        <div>
          <div className="flex justify-between text-sm mb-2">
            <span className="text-gray-400">Disk</span>
            <span
              className={
                diskStatus === "critical"
                  ? "text-red-400"
                  : diskStatus === "warning"
                    ? "text-yellow-400"
                    : "text-green-400"
              }
            >
              {metrics.system.disk_usage_percent.toFixed(1)}%
            </span>
          </div>
          <ProgressBar
            value={metrics.system.disk_usage_percent}
            color={
              diskStatus === "critical"
                ? "bg-red-500"
                : diskStatus === "warning"
                  ? "bg-yellow-500"
                  : "bg-green-500"
            }
          />
        </div>
      </div>

      <div>
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
          Trading Activity
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <MetricCard label="Open Positions" value={metrics.trading.open_positions} status="good" />
          <MetricCard
            label="Total P&L"
            value={`$${metrics.trading.total_pnl.toFixed(2)}`}
            status={metrics.trading.total_pnl >= 0 ? "good" : "critical"}
          />
          <MetricCard label="Trades Today" value={metrics.trading.trades_today} status="good" />
          <MetricCard
            label="Signal Queue"
            value={metrics.trading.signal_queue_size}
            status={metrics.trading.signal_queue_size > 10 ? "warning" : "good"}
          />
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
          MT5 Connection
        </h3>
        <div className="flex items-center gap-3">
          <div
            className={`w-3 h-3 rounded-full ${
              metrics.mt5.connected ? "bg-green-500 animate-pulse" : "bg-red-500"
            }`}
          />
          <span className={metrics.mt5.connected ? "text-green-400" : "text-red-400"}>
            {metrics.mt5.connected ? "Connected" : "Disconnected"}
          </span>
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide mb-4">
          API Performance
        </h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <span className="text-xs text-gray-500">Total Requests</span>
            <div className="text-2xl font-bold text-white font-mono">
              {metrics.api.total_requests.toLocaleString()}
            </div>
          </div>
          <div>
            <span className="text-xs text-gray-500">Total Errors</span>
            <div
              className={`text-2xl font-bold font-mono ${
                metrics.api.total_errors > 0 ? "text-red-400" : "text-green-400"
              }`}
            >
              {metrics.api.total_errors.toLocaleString()}
            </div>
          </div>
          <div>
            <span className="text-xs text-gray-500">Error Rate</span>
            <div
              className={`text-2xl font-bold font-mono ${
                metrics.api.total_requests > 0 &&
                metrics.api.total_errors / metrics.api.total_requests > 0.05
                  ? "text-red-400"
                  : "text-green-400"
              }`}
            >
              {metrics.api.total_requests > 0
                ? ((metrics.api.total_errors / metrics.api.total_requests) * 100).toFixed(2)
                : "0.00"}
              %
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
