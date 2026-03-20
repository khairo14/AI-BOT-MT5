"use client";
import { useEffect } from "react";
import { fetchAccount, fetchAppConfig, fetchSignals, fetchPositions } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import { useWebSocket } from "@/hooks/useWebSocket";

// all symbols that could be streamed across all modes (must match XM broker names)
const ALL_SYMBOLS = [
  "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURJPY","GBPJPY",
  "GOLD","SILVER","OILCash","BRENTCash","NGASCash",
  "US30Cash","US100Cash","US500Cash","GER40Cash","UK100Cash",
  "BTCUSD","ETHUSD","XRPUSD","SOLUSD",
  "Tesla","Nvidia","Apple","Microsoft","Amazon","Google","Facebook","Netflix","AdvMicroDev",
];

export default function AppBootstrap() {
  const { setAccount, setConfig, setSignals, setPositions } = useBotStore();

  // boot: fetch initial data + request browser notification permission
  useEffect(() => {
    // Request browser notification permission once
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission();
    }

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
