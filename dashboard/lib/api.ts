import axios from "axios";
import type {
  AccountInfo,
  OHLCVBar,
  Position,
  Signal,
  PlaceOrderRequest,
  AppConfig,
  RiskConfig,
  TradingMode,
  ExecutionMode,
  JournalEntry,
  JournalStatsResponse,
  SwitchModeResponse,
  AnalyticsPerformance,
} from "@/types";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export const api = axios.create({ baseURL: BASE, timeout: 10_000 });

// ── Account ----------------------------------------------------------------
export const fetchAccount = (): Promise<AccountInfo> =>
  api.get("/account/").then((r) => r.data);

export const fetchAccountMode = (): Promise<{ mode: "paper" | "live" }> =>
  api.get("/account/mode").then((r) => r.data);

export const switchMode = (mode: "paper" | "live", force = false): Promise<SwitchModeResponse> =>
  api.post("/account/switch-mode", { mode, force }).then((r) => r.data);

export const fetchPrice = (symbol: string): Promise<{ bid: number; ask: number }> =>
  api.get(`/account/price/${symbol}`).then((r) => r.data);

// ── Trades -----------------------------------------------------------------
export const fetchPositions = (): Promise<Position[]> =>
  api.get("/trades/positions").then((r) => r.data);

export const fetchHistory = (limit = 50): Promise<Position[]> =>
  api.get(`/trades/history?limit=${limit}`).then((r) => r.data);

export const fetchOHLCV = (
  symbol: string,
  timeframe: string,
  count = 300
): Promise<OHLCVBar[]> =>
  api.get(`/trades/ohlcv/${symbol}/${timeframe}?count=${count}`).then((r) => r.data);

export const placeOrder = (req: PlaceOrderRequest) =>
  api.post("/trades/place", req).then((r) => r.data);

export const closePosition = (ticket: number) =>
  api.post(`/trades/close/${ticket}`).then((r) => r.data);

export const closeAll = () =>
  api.post("/trades/close-all").then((r) => r.data);

export const modifyPosition = (
  ticket: number,
  sl: number,
  tp: number
) => api.patch(`/trades/modify/${ticket}`, { sl, tp }).then((r) => r.data);

// ── Signals ----------------------------------------------------------------
function normalizeSignal(raw: Record<string, unknown>): Signal {
  return {
    id:               raw.id as string,
    symbol:           raw.symbol as string,
    direction:        ((raw.direction as string) ?? "buy").toLowerCase() as "buy" | "sell",
    strategy:         (raw.strategy as string) ?? "",
    mode:             ((raw.trading_mode ?? raw.mode) as Signal["mode"]),
    entry:            ((raw.entry_price ?? raw.entry) as number) ?? 0,
    sl:               (raw.sl as number) ?? 0,
    tp:               (raw.tp as number) ?? 0,
    confidence:       (raw.confidence as number) ?? 0,
    timestamp:        ((raw.created_at ?? raw.timestamp) as string) ?? new Date().toISOString(),
    status:           (raw.status as Signal["status"]) ?? "pending",
    reason:           (raw.rejection_reason ?? raw.reason) as string | undefined,
    rejection_reason: raw.rejection_reason as string | undefined,
  };
}

export const fetchSignals = (mode?: TradingMode, status?: string): Promise<Signal[]> => {
  const params = new URLSearchParams();
  if (mode) params.set("trading_mode", mode);
  if (status) params.set("status", status);
  return api
    .get(`/signals/?${params.toString()}`)
    .then((r) => (r.data as Record<string, unknown>[]).map(normalizeSignal));
};

export const approveSignal = (id: string) =>
  api.post(`/signals/${id}/approve`).then((r) => r.data);

export const rejectSignal = (id: string, reason?: string) =>
  api.post(`/signals/${id}/reject`, { reason }).then((r) => r.data);

// ── Config -----------------------------------------------------------------
export const fetchAppConfig = (): Promise<AppConfig> =>
  api.get("/config/app").then((r) => r.data);

export const fetchRiskConfig = (): Promise<RiskConfig> =>
  api.get("/config/risk").then((r) => r.data);

export const fetchExecutionMode = (): Promise<Record<string, string>> =>
  api.get("/config/execution-mode").then((r) => r.data);

export const setExecutionMode = (mode: TradingMode, execution: ExecutionMode) =>
  api.patch(`/config/execution-mode/${mode}?mode=${execution}`).then((r) => r.data);

export const patchRiskConfig = (patch: Partial<RiskConfig>) =>
  api.patch("/config/risk", { data: patch }).then((r) => r.data);

export const patchAppConfig = (patch: Record<string, unknown>) =>
  api.patch("/config/app", { data: patch }).then((r) => r.data);

// ── Trade Journal (Phase 9) ------------------------------------------------
export const fetchTradeJournal = (
  account: "paper" | "live" | "all" = "all",
  tradingType?: TradingMode,
  limit = 50
): Promise<{ entries: JournalEntry[]; count: number }> => {
  const params = new URLSearchParams({ account, limit: String(limit) });
  if (tradingType) params.set("trading_type", tradingType);
  return api.get(`/trades/journal?${params.toString()}`).then((r) => r.data);
};

export const fetchJournalStats = (): Promise<JournalStatsResponse> =>
  api.get("/trades/journal/stats").then((r) => r.data);

// -- Logs ------------------------------------------------------------------
export const fetchLogTail = (n = 200): Promise<{ lines: string[] }> =>
  api.get(`/logs/tail?n=${n}`).then((r) => r.data);

// ── AI / ML ----------------------------------------------------------------
export const fetchAIStatus = () =>
  api.get("/ai/status").then((r) => r.data);

export const fetchRLStatus = () =>
  api.get("/ai/rl/status").then((r) => r.data);

export const fetchRLQTable = (tradingType: "scalping" | "day_trading" | "swing") =>
  api.get(`/ai/rl/qtable/${tradingType}`).then((r) => r.data);

export const fetchRLHistory = (tradingType: "scalping" | "day_trading" | "swing", days = 7, strategyName?: string) => {
  const params = new URLSearchParams({ days: String(days) });
  if (strategyName) params.set("strategy_name", strategyName);
  return api.get(`/ai/rl/history/${tradingType}?${params}`).then((r) => r.data);
};

export const fetchRLWinRateByState = (tradingType: "scalping" | "day_trading" | "swing", minSamples = 5, strategyName?: string) => {
  const params = new URLSearchParams({ min_samples: String(minSamples) });
  if (strategyName) params.set("strategy_name", strategyName);
  return api.get(`/ai/rl/win-rate-by-state/${tradingType}?${params}`).then((r) => r.data);
};

export const resetRLAgent = (trading_type: string) =>
  api.post(`/ai/rl/reset/${trading_type}`).then((r) => r.data);

export const fetchMemoryStats = (tradingType?: string) => {
  const params = tradingType ? `?trading_type=${tradingType}` : "";
  return api.get(`/ai/memory/stats${params}`).then((r) => r.data);
};

export const fetchOptimizerStatus = () =>
  api.get("/ai/optimizer/status").then((r) => r.data);

export const trainSymbol = (symbol: string, tradingType: string, bars = 0) =>
  api.post(`/ai/train/${symbol}`, { trading_type: tradingType, bars }).then((r) => r.data);

export const trainAllSymbols = (bars = 0) =>
  api.post("/ai/train/all", { bars }).then((r) => r.data);

export const calibrateAllModels = () =>
  api.post("/ai/models/calibrate/all").then((r) => r.data);

export const resetCalibration = () =>
  api.delete("/ai/models/calibrate/reset").then((r) => r.data);

export const runOptimizer = (strategy: string, symbol: string, tradingType: string, bars = 0) =>
  api.post(`/ai/optimizer/run/${strategy}/${symbol}`, { trading_type: tradingType, bars }).then((r) => r.data);

export const runOptimizerAll = (bars = 0) =>
  api.post("/ai/optimizer/run/all", { bars }).then((r) => r.data);

// ── Risk / Circuit Breaker -------------------------------------------------
export const fetchRiskStatus = () =>
  api.get("/risk/status").then((r) => r.data);

export const resetDrawdown = () =>
  api.post("/risk/reset-drawdown").then((r) => r.data);

export const fetchStrategyStatus = (): Promise<Record<string, { consecutive_losses: number; paused_until: string | null }>> =>
  api.get("/risk/strategy-status").then((r) => r.data);

// ── Notifications ----------------------------------------------------------
import type { Notification } from "@/types";

export const fetchNotifications = (unreadOnly = false, type?: string): Promise<Notification[]> => {
  const params = new URLSearchParams();
  if (unreadOnly) params.set("unread_only", "true");
  if (type) params.set("type", type);
  const qs = params.toString();
  return api.get(`/notifications${qs ? `?${qs}` : ""}`).then((r) => r.data.notifications || r.data);
};

export const getNotificationStats = () =>
  api.get("/notifications/stats").then((r) => r.data);

export const markNotificationRead = (id: string) =>
  api.post(`/notifications/${id}/read`).then((r) => r.data);

export const markAllNotificationsRead = () =>
  api.post("/notifications/mark-all-read").then((r) => r.data);

// ── Execution Quality ------------------------------------------------------
import type { ExecutionQualityMetrics } from "@/types";

export const fetchExecutionQuality = (): Promise<ExecutionQualityMetrics> =>
  api.get("/execution-quality/metrics").then((r) => r.data);

export const deleteNotification = (id: string) =>
  api.delete(`/notifications/${id}`).then((r) => r.data);

export const clearAllNotifications = () =>
  api.delete("/notifications").then((r) => r.data);

// ── Portfolio Optimization -------------------------------------------------
import type { PortfolioStatus, PortfolioOptimalAllocation } from "@/types";

export const fetchPortfolioStatus = (): Promise<PortfolioStatus> =>
  api.get("/portfolio/status").then((r) => r.data);

export const fetchPortfolioOptimalAllocation = (minTrades?: number): Promise<PortfolioOptimalAllocation> => {
  const params = minTrades ? `?min_trades_required=${minTrades}` : "";
  return api.get(`/portfolio/optimal-allocation${params}`).then((r) => r.data);
};

export const togglePortfolioOptimization = (enabled: boolean): Promise<PortfolioStatus> =>
  api.patch("/config/app", { 
    data: { portfolio_optimization: { enabled } } 
  }).then(() => fetchPortfolioStatus());

// ── Monitoring -------------------------------------------------------------
export const fetchPrometheusMetrics = (): Promise<string> =>
  api.get("/metrics", { 
    headers: { Accept: "text/plain" },
    transformResponse: [(data) => data] // Don't parse as JSON
  }).then((r) => r.data);

// Parse Prometheus text format to structured data
export const fetchMonitoringMetrics = async () => {
  const text = await fetchPrometheusMetrics();
  const lines = text.split("\n");
  const metrics: Record<string, number> = {};
  
  for (const line of lines) {
    if (line.startsWith("#") || !line.trim()) continue;
    const match = line.match(/^(\w+)(?:\{[^}]*\})?\s+([\d.]+)$/);
    if (match) {
      const [, name, value] = match;
      metrics[name] = parseFloat(value);
    }
  }
  
  return {
    system: {
      cpu_usage_percent: metrics.aibot_cpu_usage_percent || 0,
      memory_usage_percent: metrics.aibot_memory_usage_percent || 0,
      disk_usage_percent: metrics.aibot_disk_usage_percent || 0,
      thread_count: metrics.aibot_thread_count || 0,
    },
    trading: {
      open_positions: metrics.aibot_open_positions || 0,
      total_pnl: metrics.aibot_total_pnl || 0,
      trades_today: metrics.aibot_trades_today || 0,
      signal_queue_size: metrics.aibot_signal_queue_size || 0,
    },
    mt5: {
      connected: (metrics.aibot_mt5_connected || 0) === 1,
    },
    api: {
      total_requests: metrics.aibot_api_requests_total || 0,
      total_errors: metrics.aibot_api_errors_total || 0,
      avg_latency_ms: 0, // Calculated from endpoint data
      endpoints: {},
    },
  };
};

export const resetConsecutiveLosses = () =>
  api.post("/risk/reset-consecutive-losses").then((r) => r.data);

export const toggleCircuitBreaker = (enabled: boolean) =>
  api.post("/risk/circuit-breaker", { enabled }).then((r) => r.data);

export interface HealthResponse {
  status: "healthy" | "degraded";
  timestamp: string;
  version: string;
  checks: {
    mt5_connection: "ok" | "disconnected" | string;
    disk_space: "ok" | "warning" | string;
    memory: "ok" | "warning" | string;
    risk_manager: "ok" | "circuit_breaker_active" | "not_initialized" | string;
  };
  metrics: {
    disk_free_gb: number;
    disk_usage_pct: number;
    memory_usage_pct: number;
    memory_available_gb: number;
    circuit_breaker_active: boolean;
  };
  trading_mode: string;
  correlation_id?: string | null;
}

export const fetchHealth = () =>
  api.get("/health").then((r) => r.data as HealthResponse);

export const reconnectMT5 = () =>
  api.post("/account/reconnect").then((r) => r.data);

// ── Backtest ----------------------------------------------------------------
export interface BacktestRequest {
  symbol:          string;
  strategy:        string;
  trading_type:    "scalping" | "day_trading" | "swing";
  bars?:           number;
  initial_balance?: number;
  risk_pct?:       number;
  use_ai_filters?: boolean;  // GAP-BT-2
}

export const fetchBacktestStrategies = () =>
  api.get("/backtest/strategies").then((r) => r.data as Record<string, string[]>);

export const runBacktest = (req: BacktestRequest) =>
  api.post("/backtest/run", req, { timeout: 120_000 }).then((r) => r.data);

export const fetchBacktestHistory = (params?: {
  page?: number; page_size?: number;
  trading_type?: string; symbol?: string; strategy?: string;
}) => api.get("/backtest/history", { params }).then((r) => r.data);

export const fetchBacktestRun = (id: string) =>
  api.get(`/backtest/history/${id}`).then((r) => r.data);

export const deleteBacktestRun = (id: string) =>
  api.delete(`/backtest/history/${id}`).then((r) => r.data);

// ── Scanner ----------------------------------------------------------------
export interface ScannerModeConfig {
  enabled: boolean;
  symbols: string[];
  timeframe: string;
}

export const fetchScannerConfig = (): Promise<Record<string, ScannerModeConfig>> =>
  api.get("/config/scanner").then((r) => r.data);

export const patchScannerConfig = (patch: Record<string, unknown>) =>
  api.patch("/config/scanner", { data: patch }).then((r) => r.data);

export interface AvailableSymbolsResponse {
  symbols: string[];
  grouped: Record<string, string[]>;
}

export const fetchAvailableSymbols = (): Promise<AvailableSymbolsResponse> =>
  api.get("/config/available-symbols").then((r) => r.data);

// ── Market Scanner (New) ---------------------------------------------------
export interface ScanResult {
  symbol: string;
  category: string;
  trading_type: string;
  atr_pips: number;
  spread_pips: number;
  adx: number;
  daily_volume: number;
  volatility_percentile: number;
  liquidity_score: number;
  momentum_score: number;
  volatility_score: number;
  spread_score: number;
  trend_score: number;
  liquidity_subscore: number;
  momentum_subscore: number;
  composite_score: number;
  current_price: number;
  pip_value: number;
  trading_hours_active: boolean;
  last_updated: string;
}

export interface ScanSummary {
  timestamp: string;
  trading_types: Record<string, ScanResult[]>;
  total_scanned: number;
  total_passed: number;
  scan_duration_seconds: number;
  config_hash: string;
}

export const fetchScanResults = (forceRefresh = false): Promise<{ status: string; data: ScanSummary }> =>
  api.get(`/scanner/?force_refresh=${forceRefresh}`).then((r) => r.data);

export const fetchScanByType = (
  tradingType: "scalping" | "day_trading" | "swing",
  forceRefresh = false
): Promise<{ status: string; trading_type: string; count: number; results: ScanResult[] }> =>
  api.get(`/scanner/type/${tradingType}?force_refresh=${forceRefresh}`).then((r) => r.data);

export const triggerScan = (tradingType?: string, forceRefresh = true) =>
  api.post("/scanner/scan", { trading_type: tradingType, force_refresh: forceRefresh }).then((r) => r.data);

export const fetchScannerCacheStatus = () =>
  api.get("/scanner/cache").then((r) => r.data);

export const invalidateScannerCache = () =>
  api.post("/scanner/cache/invalidate").then((r) => r.data);

export const addSymbolToConfig = (symbol: string, tradingType: string, enabled = true) =>
  api.post("/scanner/add-symbol", { symbol, trading_type: tradingType, enabled }).then((r) => r.data);

export const fetchScannerSystemConfig = () =>
  api.get("/scanner/config").then((r) => r.data);

export const fetchScannerHealth = () =>
  api.get("/scanner/health").then((r) => r.data);

export const fetchScannerPerformance = (): Promise<{
  status: string;
  timestamp: string;
  trading_types: Record<string, {
    enabled: boolean;
    total_active: number;
    total_signals: number;
    total_trades: number;
    total_profit: number;
    avg_win_rate: number;
    active_pairs: Array<{
      symbol: string;
      signals: number;
      trades: number;
      wins: number;
      losses: number;
      win_rate: number;
      total_profit: number;
      last_signal: string | null;
      status: string;
    }>;
  }>;
}> => api.get("/scanner/performance").then((r) => r.data);

// ── Analytics ---------------------------------------------------------------
export const fetchAnalyticsPerformance = (
  account: "paper" | "live" | "all" = "all",
  tradingType: "scalping" | "day_trading" | "swing" | "all" = "all",
  limit = 5000,
): Promise<AnalyticsPerformance> => {
  const params = new URLSearchParams({
    account,
    trading_type: tradingType,
    limit: String(limit),
  });
  return api.get(`/analytics/performance?${params.toString()}`).then((r) => r.data);
};

// ── Profitability Reporting ------------------------------------------------
export interface ProfitabilityReport {
  generated_at: string;
  period_days: number | "all";
  overall: {
    total_trades: number;
    wins: number;
    losses: number;
    breakeven: number;
    win_rate: number;
    profit_factor: number;
    total_profit: number;
    avg_profit: number;
    avg_loss: number;
  };
  by_symbol: Record<string, {
    total_trades: number;
    wins: number;
    losses: number;
    breakeven: number;
    win_rate: number;
    profit_factor: number;
    total_profit: number;
    avg_profit: number;
    avg_loss: number;
  }>;
  by_trading_type: Record<string, {
    total_trades: number;
    wins: number;
    losses: number;
    breakeven: number;
    win_rate: number;
    profit_factor: number;
    total_profit: number;
    avg_profit: number;
    avg_loss: number;
  }>;
  success_criteria: {
    "50_trades": boolean;
    win_rate_50: boolean;
    win_rate_55: boolean;
    profit_factor_1_5: boolean;
    all_criteria_met: boolean;
  };
  task_2_status: {
    status: "PASSED" | "IN_PROGRESS";
    message: string;
    recommendation: string;
  };
}

export const fetchProfitabilityReport = (days?: number): Promise<ProfitabilityReport> =>
  api.get(`/profitability/${days ? `?days=${days}` : ""}`).then((r) => r.data);

export const fetchProfitabilityStatus = (): Promise<{
  task: string;
  status: "PASSED" | "IN_PROGRESS";
  message: string;
  overall_metrics: ProfitabilityReport["overall"];
  success_criteria: ProfitabilityReport["success_criteria"];
}> => api.get("/profitability/status").then((r) => r.data);

export const fetchRegimeStatus = (): Promise<{ regimes: Record<string, string> }> =>
  api.get("/analytics/regime/status").then((r) => r.data);

export const fetchLstmCalibration = (minSamples = 5) =>
  api.get(`/ai/lstm/calibration?min_samples=${minSamples}`).then((r) => r.data);

export const fetchLstmAccuracyHistory = (tradingType?: string, days = 30) =>
  api.get(`/ai/lstm/accuracy/history?days=${days}${tradingType ? `&trading_type=${tradingType}` : ""}`).then((r) => r.data);

export const fetchLstmConfidenceDistribution = (tradingType?: string) =>
  api.get(`/ai/lstm/confidence-distribution${tradingType ? `?trading_type=${tradingType}` : ""}`).then((r) => r.data);
