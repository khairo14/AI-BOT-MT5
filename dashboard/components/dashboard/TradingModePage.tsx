"use client";
import { useState, useEffect, useCallback } from "react";
import dynamic from "next/dynamic";
import { fetchOHLCV, fetchPositions } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import AccountSummary from "@/components/dashboard/AccountSummary";
import TradePanel from "@/components/trading/TradePanel";
import PositionTable from "@/components/trading/PositionTable";
import SignalQueue from "@/components/trading/SignalQueue";
import type { OHLCVBar, TradingMode } from "@/types";

// Dynamic import to prevent SSR for TradingView chart
const TradingChart = dynamic(() => import("@/components/chart/TradingChart"), {
  ssr: false,
  loading: () => <div className="w-full h-96 bg-gray-900 rounded-xl animate-pulse" />,
});

interface Props {
  mode: TradingMode;
  label: string;
  icon: string;
  symbols: string[];
  defaultSymbol: string;
  defaultTimeframe: string;
  timeframes: string[];
  strategyNames: string[];
}

export default function TradingModePage({
  mode,
  label,
  icon,
  symbols,
  defaultSymbol,
  defaultTimeframe,
  timeframes,
  strategyNames,
}: Props) {
  const { account, positions, ticks, setPositions } = useBotStore();
  const [symbol, setSymbol] = useState(defaultSymbol);
  const [timeframe, setTimeframe] = useState(defaultTimeframe);
  const [bars, setBars] = useState<OHLCVBar[]>([]);
  const [tab, setTab] = useState<"positions" | "signals">("signals");

  const modePositions = positions.filter((p) =>
    symbols.includes(p.symbol)
  );

  const loadChart = useCallback(() => {
    fetchOHLCV(symbol, timeframe, 300)
      .then(setBars)
      .catch(() => {});
  }, [symbol, timeframe]);

  useEffect(() => {
    loadChart();
    const interval = setInterval(loadChart, 60_000);
    return () => clearInterval(interval);
  }, [loadChart]);

  const refreshPositions = () =>
    fetchPositions().then(setPositions).catch(() => {});

  const tick = ticks[symbol];

  return (
    <div className="p-8 space-y-6 max-w-7xl">
      {/* Header */}
      <div className="flex items-center gap-3">
        <span className="text-3xl">{icon}</span>
        <div>
          <h2 className="text-2xl font-bold text-white">{label}</h2>
          <p className="text-gray-500 text-sm">
            {strategyNames.join(" · ")}
          </p>
        </div>
      </div>

      <AccountSummary account={account} />

      {/* Symbol + timeframe controls */}
      <div className="flex flex-wrap gap-3 items-center">
        <select
          value={symbol}
          onChange={(e) => setSymbol(e.target.value)}
          className="bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
        >
          {symbols.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>

        <div className="flex gap-1">
          {timeframes.map((tf) => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
                timeframe === tf
                  ? "bg-blue-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:text-white"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        {tick && (
          <div className="ml-auto flex gap-4 text-sm">
            <span className="text-gray-500">Bid</span>
            <span className="text-white font-mono">{tick.bid.toFixed(5)}</span>
            <span className="text-gray-500">Ask</span>
            <span className="text-white font-mono">{tick.ask.toFixed(5)}</span>
          </div>
        )}
      </div>

      {/* Chart + right panel */}
      <div className="grid grid-cols-1 xl:grid-cols-4 gap-6">
        {/* Chart */}
        <div className="xl:col-span-3 bg-gray-900 border border-gray-800 rounded-xl p-4">
          <TradingChart bars={bars} height={420} />
        </div>

        {/* Trade panel */}
        <div className="xl:col-span-1">
          <TradePanel mode={mode} defaultSymbol={symbol} symbols={symbols} />
        </div>
      </div>

      {/* Positions + Signals tabs */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex gap-4 mb-5 border-b border-gray-800 pb-3">
          {(["signals", "positions"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`text-sm font-medium capitalize transition-colors ${
                tab === t ? "text-white" : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {t === "positions"
                ? `Positions (${modePositions.length})`
                : "Signal Queue"}
            </button>
          ))}
        </div>

        {tab === "positions" ? (
          <PositionTable positions={modePositions} onRefresh={refreshPositions} />
        ) : (
          <SignalQueue mode={mode} />
        )}
      </div>
    </div>
  );
}
