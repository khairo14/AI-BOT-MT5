import { create } from "zustand";
import { persist } from "zustand/middleware";
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

export const useBotStore = create<BotStore>()(
  persist(
    (set) => ({
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
    set((state) => {
      const existing = state.signals.find((x) => x.id === s.id);

      if (existing) {
        return {
          signals: state.signals.map((x) =>
            x.id === s.id ? { ...x, ...s } : x
          ),
        };
      }

      return {
        signals: [s, ...state.signals].slice(0, 100),
      };
    }),
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
    set((state) => {
      const now = Date.now();

      const isDuplicate = state.notifications.some((existing) => {
        const sameContent =
          existing.title === n.title &&
          existing.message === n.message &&
          existing.type === n.type;

        return sameContent &&
          now - existing.timestamp < 10_000;
      });

      if (isDuplicate) {
        return state;
      }

      return {
        notifications: [
          {
            ...n,
            id: Math.random().toString(36).slice(2),
            timestamp: now,
          },
          ...state.notifications,
        ].slice(0, 50),
      };
    }),
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
    }),
    {
      name: "evotrade-store",
      // Only persist signals — notifications are transient and must not survive a reload
      // (persisted non-string messages caused React crash on rehydration)
      partialize: (state) => ({
        signals: state.signals,
      }),
    }
  )
);
