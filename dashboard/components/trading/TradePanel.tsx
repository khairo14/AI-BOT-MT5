"use client";
import { useState, useEffect } from "react";
import { placeOrder, fetchScannerConfig } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { TradingMode, ExecutionMode, Signal } from "@/types";

interface Props {
  mode: TradingMode;
  defaultSymbol: string;
  execMode: ExecutionMode;
}

interface SymbolOption {
  symbol: string;
  category: string;
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

// Categorize symbol for grouping in dropdown
function categorizeSymbol(sym: string): string {
  const upper = sym.toUpperCase();
  
  const forexEndings = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"];
  if (sym.length === 6 && forexEndings.some(end => upper.endsWith(end))) {
    const majors = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"];
    return majors.includes(upper) ? "Forex Majors" : "Forex Minors";
  }
  
  if (upper.includes("BTC") || upper.includes("ETH") || upper.includes("XRP") || 
      upper.includes("SOL") || upper.includes("ADA") || upper.includes("DOGE")) {
    return "Crypto";
  }
  
  if (upper.includes("US100") || upper.includes("US30") || upper.includes("US500") || 
      upper.includes("SPX") || upper.includes("NAS") || upper.includes("GER") || 
      upper.includes("UK100") || upper.includes("DAX")) {
    return "Indices";
  }
  
  if (upper.includes("GOLD") || upper.includes("XAU") || upper.includes("SILVER") || 
      upper.includes("XAG") || upper.includes("OIL") || upper.includes("BRENT") || 
      upper.includes("NGAS")) {
    return "Commodities";
  }
  
  return "Other";
}

export default function TradePanel({ mode, defaultSymbol, execMode }: Props) {
  const { ticks, signals, pushNotification } = useBotStore();
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [direction, setDirection] = useState<"buy" | "sell">("buy");
  const [sl, setSl] = useState("20");
  const [tp, setTp] = useState("40");
  const [loading, setLoading] = useState(false);
  const [scannerSymbols, setScannerSymbols] = useState<SymbolOption[]>([]);
  const [loadingSymbols, setLoadingSymbols] = useState(true);

  // Load scanner config for this mode
  useEffect(() => {
    const loadSymbols = async () => {
      setLoadingSymbols(true);
      try {
        const scannerConfig = await fetchScannerConfig();
        const modeConfig = scannerConfig[mode];
        const symbols = modeConfig?.symbols || [];
        
        // Group by category
        const grouped = new Map<string, Set<string>>();
        symbols.forEach(sym => {
          const cat = categorizeSymbol(sym);
          if (!grouped.has(cat)) grouped.set(cat, new Set());
          grouped.get(cat)!.add(sym);
        });
        
        const options: SymbolOption[] = [];
        // Sort categories by priority
        const priority = ["Forex Majors", "Forex Minors", "Crypto", "Commodities", "Indices", "Other"];
        for (const cat of priority) {
          if (grouped.has(cat)) {
            const syms = Array.from(grouped.get(cat)!).sort();
            syms.forEach(s => options.push({ symbol: s, category: cat }));
          }
        }
        setScannerSymbols(options);
        
        // Set default symbol if current symbol not in list
        if (symbols.length > 0 && !symbols.includes(symbol)) {
          setSymbol(symbols[0]);
        }
      } catch (err) {
        console.error("Failed to load scanner symbols:", err);
      } finally {
        setLoadingSymbols(false);
      }
    };
    loadSymbols();
  }, [mode, defaultSymbol]);

  const tick = ticks[symbol];

  // Find the top pending signal for the current symbol in this mode
  const activeSignal: Signal | undefined = signals.find(
    (s) => s.mode === mode && s.symbol === symbol && s.status === "pending"
  );

  // When a signal arrives for this symbol, pre-fill the form
  useEffect(() => {
    if (!activeSignal) return;
    setDirection(activeSignal.direction);
    if (tick && activeSignal.entry && activeSignal.sl && activeSignal.tp) {
      const pipFactor = symbol.includes("JPY") ? 100 : 10000;
      const slPips = Math.abs(activeSignal.entry - activeSignal.sl) * pipFactor;
      const tpPips = Math.abs(activeSignal.tp - activeSignal.entry) * pipFactor;
      setSl(slPips.toFixed(1));
      setTp(tpPips.toFixed(1));
    }
  }, [activeSignal?.id]);

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
      const raw = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      const msg = typeof raw === "string" ? raw : raw != null ? JSON.stringify(raw) : "Order failed";
      pushNotification({ type: "error", title: "Order error", message: msg });
    } finally {
      setLoading(false);
    }
  };

  // Group symbols by category for display
  const groupedSymbols: Map<string, string[]> = new Map();
  scannerSymbols.forEach(({ symbol: sym, category }) => {
    if (!groupedSymbols.has(category)) groupedSymbols.set(category, []);
    groupedSymbols.get(category)!.push(sym);
  });

  if (loadingSymbols) {
    return (
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 space-y-4">
        <div className="animate-pulse">
          <div className="h-4 bg-gray-800 rounded w-24 mb-4"></div>
          <div className="h-10 bg-gray-800 rounded mb-3"></div>
          <div className="h-10 bg-gray-800 rounded"></div>
        </div>
      </div>
    );
  }

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
          {Array.from(groupedSymbols.entries()).map(([category, syms]) => (
            <optgroup key={category} label={category}>
              {syms.map((s) => (
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

      {scannerSymbols.length > 0 && (
        <p className="text-[10px] text-gray-700 text-center">
          {scannerSymbols.length} symbols available · {mode.replace("_", " ")}
        </p>
      )}
    </div>
  );
}