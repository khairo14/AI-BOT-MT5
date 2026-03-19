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
export const fetchSignals = (mode?: TradingMode, status?: string): Promise<Signal[]> => {
  const params = new URLSearchParams();
  if (mode) params.set("mode", mode);
  if (status) params.set("status", status);
  return api.get(`/signals/?${params.toString()}`).then((r) => r.data);
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
