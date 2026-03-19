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

export const fetchMemoryStats = (tradingType?: string) => {
  const params = tradingType ? `?trading_type=${tradingType}` : "";
  return api.get(`/ai/memory/stats${params}`).then((r) => r.data);
};

export const fetchOptimizerStatus = () =>
  api.get("/ai/optimizer/status").then((r) => r.data);

export const trainSymbol = (symbol: string, tradingType: string, bars = 1000) =>
  api.post(`/ai/train/${symbol}`, { trading_type: tradingType, bars }).then((r) => r.data);

export const trainAllSymbols = (bars = 1000) =>
  api.post("/ai/train/all", { bars }).then((r) => r.data);

export const runOptimizer = (strategy: string, symbol: string, tradingType: string, bars = 1500) =>
  api.post(`/ai/optimizer/run/${strategy}/${symbol}`, { trading_type: tradingType, bars }).then((r) => r.data);

export const runOptimizerAll = (bars = 1500) =>
  api.post("/ai/optimizer/run/all", { bars }).then((r) => r.data);

// ── Backtest ----------------------------------------------------------------
export interface BacktestRequest {
  symbol:          string;
  strategy:        string;
  trading_type:    "scalping" | "day_trading" | "swing";
  bars?:           number;
  initial_balance?: number;
  risk_pct?:       number;
}

export const fetchBacktestStrategies = () =>
  api.get("/backtest/strategies").then((r) => r.data as Record<string, string[]>);

export const runBacktest = (req: BacktestRequest) =>
  api.post("/backtest/run", req).then((r) => r.data);

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
