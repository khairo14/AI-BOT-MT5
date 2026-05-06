"use client";
import { useEffect, useState } from "react";
import { fetchAccount, fetchAppConfig, fetchSignals, fetchPositions, fetchScannerConfig } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import { useWebSocket } from "@/hooks/useWebSocket";

export default function AppBootstrap() {
  const { setAccount, setConfig, setSignals, setPositions } = useBotStore();
  const [wsSymbols, setWsSymbols] = useState<string[]>([]);

  // Load scanner config and extract symbols for WebSocket
  useEffect(() => {
    const loadScannerSymbols = async () => {
      try {
        const scannerConfig = await fetchScannerConfig();
        const allSymbols = [
          ...(scannerConfig.scalping?.symbols || []),
          ...(scannerConfig.day_trading?.symbols || []),
          ...(scannerConfig.swing?.symbols || []),
        ];
        setWsSymbols([...new Set(allSymbols)]);
      } catch (err) {
        console.error("Failed to load scanner symbols:", err);
        setWsSymbols([]);
      }
    };
    loadScannerSymbols();
  }, []);

  // boot: fetch initial data + request browser notification permission
  useEffect(() => {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission();
    }

    fetchAccount().then(setAccount).catch(() => {});
    fetchAppConfig().then(setConfig).catch(() => {});
    fetchSignals().then(setSignals).catch(() => {});
    fetchPositions().then(setPositions).catch(() => {});

    const interval = setInterval(() => {
      fetchAccount().then(setAccount).catch(() => {});
      fetchPositions().then(setPositions).catch(() => {});
      fetchSignals().then(setSignals).catch(() => {});
    }, 5000);

    return () => clearInterval(interval);
  }, []);

  // Only connect WebSocket if we have symbols to subscribe to
  useWebSocket(wsSymbols);

  return null;
}