// ── Account ────────────────────────────────────────────────────────────────
export interface AccountInfo {
  login: number;
  server: string;
  currency: string;
  balance: number;
  equity: number;
  margin: number;
  free_margin: number;
  margin_level: number;
  profit: number;
  leverage: number;
  mode: "paper" | "live";
}

// ── OHLCV ──────────────────────────────────────────────────────────────────
export interface OHLCVBar {
  time: string; // ISO string
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

// ── Positions ──────────────────────────────────────────────────────────────
export interface Position {
  ticket: number;
  symbol: string;
  type: "buy" | "sell";
  volume: number;
  open_price: number;
  sl: number;
  tp: number;
  profit: number;
  swap: number;
  open_time: string;
  comment: string;
  magic: number;
}

// ── Signals ────────────────────────────────────────────────────────────────
export type SignalStatus = "pending" | "approved" | "rejected" | "executed" | "executing" | "failed";

export interface Signal {
  id: string;
  symbol: string;
  direction: "buy" | "sell";
  strategy: string;
  mode: "scalping" | "day_trading" | "swing";
  entry: number;
  sl: number;
  tp: number;
  confidence: number;
  timestamp: string;
  status: SignalStatus;
  reason?: string;
  rejection_reason?: string;
}

// ── WebSocket ──────────────────────────────────────────────────────────────
export interface TickData {
  symbol: string;
  bid: number;
  ask: number;
  time: string;
}

export interface WSMessage {
  type: "tick" | "positions" | "signal" | "pong";
  data: TickData | Position[] | Signal | null;
}

// ── Trade panel ────────────────────────────────────────────────────────────
export type TradingMode = "scalping" | "day_trading" | "swing";

export interface PlaceOrderRequest {
  symbol: string;
  direction: "buy" | "sell";
  mode: TradingMode;
  sl_pips?: number;
  tp_pips?: number;
  comment?: string;
}

// ── Config ─────────────────────────────────────────────────────────────────
export type ExecutionMode = "manual" | "auto";

export interface AppConfig {
  trading_mode: "paper" | "live";
  execution_mode: {
    scalping: ExecutionMode;
    day_trading: ExecutionMode;
    swing: ExecutionMode;
  };
}

export interface RiskConfig {
  risk_per_trade_pct: number;
  max_risk_per_trade_pct: number;
  min_rr_ratio: number;
  max_concurrent_trades: Record<TradingMode, number>;
  daily_drawdown_limit_pct: number;
  weekly_drawdown_limit_pct: number;
}

// ── Trade Journal (Phase 9) ────────────────────────────────────────────────
export type AccountMode = "paper" | "live";

export interface JournalEntry {
  ticket: number;
  symbol: string;
  direction: "buy" | "sell";
  volume: number;
  entry: number;
  sl: number;
  tp: number | null;
  profit: number | null;
  trading_type: TradingMode;
  account_mode: AccountMode;
  comment: string;
  event: "open" | "close";
  open_time: string;
  close_time: string | null;
  logged_at: string;
}

export interface JournalStats {
  total: number;
  wins: number;
  losses: number;
  win_rate: number;
  total_profit: number;
}

export interface JournalStatsResponse {
  paper: JournalStats;
  live: JournalStats;
  all: JournalStats;
}

export interface SwitchModeResponse {
  status: "switched" | "no_change";
  mode: AccountMode;
  account?: AccountInfo;
}

export interface SwitchModeError {
  error: "open_positions";
  message: string;
  open_count: number;
}
