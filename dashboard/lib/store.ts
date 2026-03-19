import { create } from "zustand";
import type {
  AccountInfo,
  Position,
  Signal,
  TickData,
  TradingMode,
  AppConfig,
} from "@/types";

interface Notification {
  id: string;
  type: "info" | "success" | "warning" | "error";
  title: string;
  message: string;
  timestamp: number;
}

interface BotStore {
  // Account
  account: AccountInfo | null;
  setAccount: (a: AccountInfo) => void;

  // Live ticks  { [symbol]: TickData }
  ticks: Record<string, TickData>;
  setTick: (t: TickData) => void;

  // Open positions
  positions: Position[];
  setPositions: (p: Position[]) => void;

  // Pending signals
  signals: Signal[];
  setSignals: (s: Signal[]) => void;
  addSignal: (s: Signal) => void;
  updateSignalStatus: (id: string, status: Signal["status"]) => void;
  updateSignal: (id: string, fields: Partial<Signal>) => void;

  // App config
  config: AppConfig | null;
  setConfig: (c: AppConfig) => void;

  // Active trading mode (for per-mode dashboard context)
  activeMode: TradingMode;
  setActiveMode: (m: TradingMode) => void;

  // In-app notifications
  notifications: Notification[];
  pushNotification: (n: Omit<Notification, "id" | "timestamp">) => void;
  dismissNotification: (id: string) => void;

  // WebSocket status
  wsConnected: boolean;
  setWsConnected: (v: boolean) => void;

  // API log console
  logLines: string[];
  appendLogLines: (lines: string[]) => void;
  setLogLines: (lines: string[]) => void;
}

export const useBotStore = create<BotStore>((set) => ({
  account: null,
  setAccount: (account) => set({ account }),

  ticks: {},
  setTick: (t) =>
    set((s) => ({ ticks: { ...s.ticks, [t.symbol]: t } })),

  positions: [],
  setPositions: (positions) => set({ positions }),

  signals: [],
  setSignals: (signals) => set({ signals }),
  addSignal: (s) =>
    set((state) => ({
      signals: [s, ...state.signals].slice(0, 100),
    })),
  updateSignalStatus: (id, status) =>
    set((state) => ({
      signals: state.signals.map((s) => (s.id === id ? { ...s, status } : s)),
    })),
  updateSignal: (id, fields) =>
    set((state) => ({
      signals: state.signals.map((s) => (s.id === id ? { ...s, ...fields } : s)),
    })),

  config: null,
  setConfig: (config) => set({ config }),

  activeMode: "scalping",
  setActiveMode: (activeMode) => set({ activeMode }),

  notifications: [],
  pushNotification: (n) =>
    set((state) => ({
      notifications: [
        {
          ...n,
          id: Math.random().toString(36).slice(2),
          timestamp: Date.now(),
        },
        ...state.notifications,
      ].slice(0, 20),
    })),
  dismissNotification: (id) =>
    set((state) => ({
      notifications: state.notifications.filter((n) => n.id !== id),
    })),

  wsConnected: false,
  setWsConnected: (wsConnected) => set({ wsConnected }),

  logLines: [] as string[],
  appendLogLines: (lines: string[]) =>
    set((state) => ({
      logLines: [...state.logLines, ...lines].slice(-500),
    })),
  setLogLines: (lines: string[]) => set({ logLines: lines }),
}));
