"use client";
import { useState, useEffect } from "react";
import { placeOrder } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { TradingMode, ExecutionMode, Signal } from "@/types";
import type { SymbolGroup } from "@/components/dashboard/TradingModePage";

interface Props {
  mode: TradingMode;
  defaultSymbol: string;
  symbolGroups: SymbolGroup[];
  execMode: ExecutionMode;
}

function ConfidenceBadge({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const color = value >= 0.75 ? "text-emerald-400 bg-emerald-950 border-emerald-800"
    : value >= 0.5 ? "text-yellow-400 bg-yellow-950 border-yellow-800"
    : "text-gray-400 bg-gray-800 border-gray-700";
  return (
    <span className={`text-xs font-bold px-2 py-0.5 rounded-full border ${color}`}>
      {pct}% conf
    </span>
  );
}

export default function TradePanel({ mode, defaultSymbol, symbolGroups, execMode }: Props) {
  const { ticks, signals, pushNotification } = useBotStore();
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [direction, setDirection] = useState<"buy" | "sell">("buy");
  const [sl, setSl] = useState("20");
  const [tp, setTp] = useState("40");
  const [loading, setLoading] = useState(false);

  const allSymbols = symbolGroups.flatMap((g) => g.symbols);
  const tick = ticks[symbol];

  // Find the top pending signal for the current symbol in this mode
  const activeSignal: Signal | undefined = signals.find(
    (s) => s.mode === mode && s.symbol === symbol && s.status === "pending"
  );

  // When a signal arrives for this symbol, pre-fill the form
  useEffect(() => {
    if (!activeSignal) return;
    setDirection(activeSignal.direction);
    // Convert absolute SL/TP to pips approximation for display
    if (tick && activeSignal.entry && activeSignal.sl && activeSignal.tp) {
      const pipFactor = symbol.includes("JPY") ? 100 : 10000;
      const slPips = Math.abs(activeSignal.entry - activeSignal.sl) * pipFactor;
      const tpPips = Math.abs(activeSignal.tp - activeSignal.entry) * pipFactor;
      setSl(slPips.toFixed(1));
      setTp(tpPips.toFixed(1));
    }
  }, [activeSignal?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleSubmit = async () => {
    setLoading(true);
    try {
      const res = await placeOrder({
        symbol,
        direction,
        mode,
        sl_pips: parseFloat(sl) || undefined,
        tp_pips: parseFloat(tp) || undefined,
      });
      pushNotification({
        type: "success",
        title: "Order placed",
        message: `${direction.toUpperCase()} ${symbol} @ ${res.price ?? "market"} — ticket #${res.ticket}`,
      });
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Order failed";
      pushNotification({ type: "error", title: "Order error", message: msg });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
        {execMode === "manual" ? "Manual Trade" : "Place Order"}
      </h3>

      {/* Strategy signal banner — only in manual mode when there's a pending signal */}
      {execMode === "manual" && activeSignal && (
        <div className={`rounded-lg border p-3 space-y-2 ${
          activeSignal.direction === "buy"
            ? "border-emerald-800 bg-emerald-950/40"
            : "border-red-800 bg-red-950/40"
        }`}>
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
                activeSignal.direction === "buy"
                  ? "bg-emerald-900 text-emerald-300"
                  : "bg-red-900 text-red-300"
              }`}>
                {activeSignal.direction.toUpperCase()} SIGNAL
              </span>
              <span className="text-xs text-gray-400">{activeSignal.strategy}</span>
            </div>
            <ConfidenceBadge value={activeSignal.confidence} />
          </div>

          <div className="grid grid-cols-3 gap-1.5 text-xs">
            <div className="bg-gray-900/60 rounded px-2 py-1.5">
              <p className="text-gray-500 text-[10px] uppercase">Entry</p>
              <p className="text-white font-mono font-semibold">{activeSignal.entry?.toFixed(5) ?? "—"}</p>
            </div>
            <div className="bg-gray-900/60 rounded px-2 py-1.5">
              <p className="text-red-400 text-[10px] uppercase">SL</p>
              <p className="text-white font-mono font-semibold">{activeSignal.sl?.toFixed(5) ?? "—"}</p>
            </div>
            <div className="bg-gray-900/60 rounded px-2 py-1.5">
              <p className="text-emerald-400 text-[10px] uppercase">TP</p>
              <p className="text-white font-mono font-semibold">{activeSignal.tp?.toFixed(5) ?? "—"}</p>
            </div>
          </div>

          {activeSignal.entry && activeSignal.sl && activeSignal.tp && (
            <p className="text-[10px] text-gray-500">
              R:R {Math.abs(activeSignal.tp - activeSignal.entry) / Math.abs(activeSignal.entry - activeSignal.sl) >= 1
                ? <span className="text-emerald-400">
                    1:{(Math.abs(activeSignal.tp - activeSignal.entry) / Math.abs(activeSignal.entry - activeSignal.sl)).toFixed(2)}
                  </span>
                : <span className="text-yellow-400">
                    1:{(Math.abs(activeSignal.tp - activeSignal.entry) / Math.abs(activeSignal.entry - activeSignal.sl)).toFixed(2)}
                  </span>
              }
              &nbsp;· Form pre-filled from signal
            </p>
          )}
        </div>
      )}

      {/* No signal notice in manual mode */}
      {execMode === "manual" && !activeSignal && (
        <div className="text-xs text-gray-600 border border-gray-800 rounded-lg px-3 py-2 text-center">
          No pending signal for {symbol} — monitoring strategies...
        </div>
      )}

      {/* Symbol selector */}
      <div>
        <label className="text-xs text-gray-500 mb-1 block">Symbol</label>
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
        >
          {symbolGroups.map((grp) => (
            <optgroup key={grp.label} label={grp.label}>
              {grp.symbols.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </optgroup>
          ))}
        </select>
      </div>

      {/* Live price */}
      {tick && (
        <div className="flex gap-3 text-sm bg-gray-800/50 rounded-lg px-3 py-2">
          <span className="text-gray-500 text-xs">Bid</span>
          <span className="text-white font-mono text-xs">{tick.bid.toFixed(5)}</span>
          <span className="text-gray-500 text-xs ml-2">Ask</span>
          <span className="text-white font-mono text-xs">{tick.ask.toFixed(5)}</span>
          <span className="text-gray-600 text-[10px] ml-auto">
            {((tick.ask - tick.bid) * (symbol.includes("JPY") ? 100 : 10000)).toFixed(1)} pts
          </span>
        </div>
      )}

      {/* Direction */}
      <div className="flex gap-2">
        <button
          onClick={() => setDirection("buy")}
          className={`flex-1 py-2 rounded-lg text-sm font-semibold transition-colors ${
            direction === "buy" ? "bg-emerald-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
          }`}
        >
          BUY
        </button>
        <button
          onClick={() => setDirection("sell")}
          className={`flex-1 py-2 rounded-lg text-sm font-semibold transition-colors ${
            direction === "sell" ? "bg-red-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700"
          }`}
        >
          SELL
        </button>
      </div>

      {/* SL / TP */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-gray-500 mb-1 block">SL (pips)</label>
          <input
            type="number"
            value={sl}
            onChange={(e) => setSl(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 mb-1 block">TP (pips)</label>
          <input
            type="number"
            value={tp}
            onChange={(e) => setTp(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </div>
      </div>

      {/* Submit */}
      <button
        onClick={handleSubmit}
        disabled={loading}
        className="w-full py-2.5 rounded-lg text-sm font-semibold transition-colors bg-blue-600 hover:bg-blue-500 text-white disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {loading ? "Placing..." : execMode === "manual" ? "Place Order" : "Execute"}
      </button>

      {allSymbols.length > 0 && (
        <p className="text-[10px] text-gray-700 text-center">
          {allSymbols.length} symbols available · {mode.replace("_", " ")}
        </p>
      )}
    </div>
  );
}


interface Props {
  mode: TradingMode;
  defaultSymbol: string;
  symbols: string[];
}

export default function TradePanel({ mode, defaultSymbol, symbols }: Props) {
  const { ticks, pushNotification } = useBotStore();
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [direction, setDirection] = useState<"buy" | "sell">("buy");
  const [sl, setSl] = useState("20");
  const [tp, setTp] = useState("40");
  const [loading, setLoading] = useState(false);

  const tick = ticks[symbol];

  const handleSubmit = async () => {
    setLoading(true);
    try {
      const res = await placeOrder({
        symbol,
        direction,
        mode,
        sl_pips: parseFloat(sl) || undefined,
        tp_pips: parseFloat(tp) || undefined,
      });
      pushNotification({
        type: "success",
        title: "Order placed",
        message: `${direction.toUpperCase()} ${symbol} @ ${res.price ?? "market"} — ticket #${res.ticket}`,
      });
    } catch (e: unknown) {
      const msg = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Order failed";
      pushNotification({ type: "error", title: "Order error", message: msg });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
      <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
        Place Order
      </h3>

      {/* Symbol selector */}
      <div>
        <label className="text-xs text-gray-500 mb-1 block">Symbol</label>
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
        >
          {symbols.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {/* Live price */}
      {tick && (
        <div className="flex gap-4 text-sm">
          <span className="text-gray-500">Bid</span>
          <span className="text-white font-mono">{tick.bid.toFixed(5)}</span>
          <span className="text-gray-500 ml-4">Ask</span>
          <span className="text-white font-mono">{tick.ask.toFixed(5)}</span>
          <span className="text-gray-600 text-xs ml-2">
            Spread: {((tick.ask - tick.bid) * 10000).toFixed(1)} pts
          </span>
        </div>
      )}

      {/* Direction */}
      <div className="flex gap-2">
        <button
          onClick={() => setDirection("buy")}
          className={`flex-1 py-2 rounded-lg text-sm font-semibold transition-colors ${
            direction === "buy" ? "bg-emerald-600 text-white" : "bg-gray-800 text-gray-400"
          }`}
        >
          BUY
        </button>
        <button
          onClick={() => setDirection("sell")}
          className={`flex-1 py-2 rounded-lg text-sm font-semibold transition-colors ${
            direction === "sell" ? "bg-red-600 text-white" : "bg-gray-800 text-gray-400"
          }`}
        >
          SELL
        </button>
      </div>

      {/* SL / TP */}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="text-xs text-gray-500 mb-1 block">SL (pips)</label>
          <input
            type="number"
            value={sl}
            onChange={(e) => setSl(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </div>
        <div>
          <label className="text-xs text-gray-500 mb-1 block">TP (pips)</label>
          <input
            type="number"
            value={tp}
            onChange={(e) => setTp(e.target.value)}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
          />
        </div>
      </div>

      {/* Submit */}
      <button
        onClick={handleSubmit}
        disabled={loading}
        className="w-full py-2.5 rounded-lg text-sm font-semibold bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white transition-colors"
      >
        {loading ? "Placing…" : `${direction.toUpperCase()} ${symbol}`}
      </button>
    </div>
  );
}
