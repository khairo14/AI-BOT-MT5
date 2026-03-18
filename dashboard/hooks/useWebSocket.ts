"use client";
import { useEffect, useRef, useCallback } from "react";
import { useBotStore } from "@/lib/store";
import type { TickData, Position, Signal } from "@/types";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8000";

export function useWebSocket(symbols: string[]) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { setTick, setPositions, addSignal, setWsConnected, pushNotification } =
    useBotStore();

  const connect = useCallback(() => {
    const symbolStr = symbols.join(",");
    const ws = new WebSocket(`${WS_URL}/ws/feed?symbols=${symbolStr}`);
    wsRef.current = ws;

    ws.onopen = () => {
      setWsConnected(true);
    };

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data as string) as {
          type: string;
          data: unknown;
        };
        switch (msg.type) {
          case "tick":
            setTick(msg.data as TickData);
            break;
          case "positions":
            setPositions(msg.data as Position[]);
            break;
          case "signal": {
            const sig = msg.data as Signal;
            addSignal(sig);
            pushNotification({
              type: "info",
              title: `New signal — ${sig.symbol}`,
              message: `${sig.direction.toUpperCase()} | ${sig.strategy} | conf: ${(sig.confidence * 100).toFixed(0)}%`,
            });
            break;
          }
        }
      } catch {
        // malformed frame — ignore
      }
    };

    ws.onclose = () => {
      setWsConnected(false);
      // reconnect after 3 seconds
      reconnectTimer.current = setTimeout(() => connect(), 3000);
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [symbols.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect();
    const ping = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ type: "ping" }));
      }
    }, 30_000);

    return () => {
      clearInterval(ping);
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);
}
