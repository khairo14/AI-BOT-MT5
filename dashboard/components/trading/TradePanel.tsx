"use client";
import { useState } from "react";
import { placeOrder } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { TradingMode } from "@/types";

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
