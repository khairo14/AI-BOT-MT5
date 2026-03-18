"use client";
import { useEffect } from "react";
import { fetchAccount, fetchAppConfig, fetchSignals, fetchPositions } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import { useWebSocket } from "@/hooks/useWebSocket";

// all symbols that could be streamed across all modes
const ALL_SYMBOLS = [
  "EURUSD","GBPUSD","USDJPY","XAUUSD","BTCUSD",
  "USOIL","GBPJPY","EURUSD","US30","SPX500",
];

export default function AppBootstrap() {
  const { setAccount, setConfig, setSignals, setPositions } = useBotStore();

  // boot: fetch initial data
  useEffect(() => {
    fetchAccount().then(setAccount).catch(() => {});
    fetchAppConfig().then(setConfig).catch(() => {});
    fetchSignals().then(setSignals).catch(() => {});
    fetchPositions().then(setPositions).catch(() => {});

    // poll account + positions every 5s
    const interval = setInterval(() => {
      fetchAccount().then(setAccount).catch(() => {});
      fetchPositions().then(setPositions).catch(() => {});
    }, 5000);

    return () => clearInterval(interval);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // WebSocket live feed
  useWebSocket(ALL_SYMBOLS);

  return null;
}
