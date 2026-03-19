"use client";
import { useEffect, useRef, useCallback } from "react";
import { useBotStore } from "@/lib/store";
import type { TickData, Position, Signal } from "@/types";

const WS_URL = process.env.NEXT_PUBLIC_WS_URL ?? "ws://127.0.0.1:8000";

// Normalize backend signal dict → frontend Signal shape
function normalizeSignal(raw: Record<string, unknown>): Signal {
  return {
    id:               raw.id as string,
    symbol:           raw.symbol as string,
    direction:        (raw.direction as string ?? "buy").toLowerCase() as "buy" | "sell",
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

export function useWebSocket(symbols: string[]) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { setTick, setPositions, addSignal, updateSignal, setWsConnected, pushNotification } =
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
            const sig = normalizeSignal(msg as unknown as Record<string, unknown>);
            addSignal(sig);
            pushNotification({
              type: sig.status === "executing" ? "info" : "info",
              title: `New signal — ${sig.symbol}`,
              message: `${sig.direction.toUpperCase()} | ${sig.strategy} | conf: ${sig.confidence != null ? (sig.confidence * 100).toFixed(0) + "%" : "—"}`,
            });
            // Browser notification (if permission granted)
            if (typeof Notification !== "undefined" && Notification.permission === "granted") {
              new Notification(`Signal: ${sig.symbol} ${sig.direction.toUpperCase()}`, {
                body: `${sig.strategy} — Entry ${sig.entry?.toFixed(5) ?? "market"}`,
                icon: "/favicon.ico",
              });
            }
            break;
          }
          case "signal_update": {
            // Outcome of an auto-executed signal — update the existing card
            const raw = msg as unknown as Record<string, unknown>;
            const id = raw.id as string;
            if (id) {
              updateSignal(id, {
                status:           (raw.status as Signal["status"]),
                rejection_reason: raw.rejection_reason as string | undefined,
                reason:           (raw.rejection_reason ?? raw.reason) as string | undefined,
              });
              if (raw.status === "executed") {
                pushNotification({
                  type: "success",
                  title: `Executed — ${raw.symbol}`,
                  message: `${(raw.direction as string ?? "").toUpperCase()} ${raw.strategy ?? ""} filled`,
                });
              } else if (raw.status === "failed") {
                pushNotification({
                  type: "error",
                  title: `Execution failed — ${raw.symbol}`,
                  message: (raw.rejection_reason as string) ?? "Order rejected",
                });
              }
            }
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
