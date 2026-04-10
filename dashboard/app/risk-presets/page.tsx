"use client";

import { useState, useEffect } from "react";
import { useBotStore } from "@/lib/store";

interface PresetConfig {
  name: string;
  description: string;
  risk_per_trade_multiplier: number;
  stop_loss_multiplier: number;
  take_profit_multiplier: number;
  max_daily_loss_pct: number;
  max_weekly_loss_pct: number;
  max_concurrent_trades: {
    scalping: number;
    day_trading: number;
    swing: number;
    total: number;
  };
  max_consecutive_losses: number;
  risk_reward_min_multiplier: number;
}

interface PresetsResponse {
  current: string;
  presets: Record<string, PresetConfig>;
}

export default function RiskPresetsPage() {
  const { pushNotification } = useBotStore();
  const [presetsData, setPresetsData] = useState<PresetsResponse | null>(null);
  const [currentPreset, setCurrentPreset] = useState<string>("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    fetchPresets();
  }, []);

  const fetchPresets = async () => {
    try {
      const response = await fetch("/api/risk/presets");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data: PresetsResponse = await response.json();
      setPresetsData(data);
      setCurrentPreset(data.current);
    } catch (error) {
      console.error("Failed to fetch risk presets:", error);
      pushNotification({
        type: "error",
        title: "Failed to load risk presets",
        message: error instanceof Error ? error.message : "Unknown error"
      });
    }
  };

  const selectPreset = async (presetName: string) => {
    setLoading(true);
    try {
      const response = await fetch("/api/risk/presets/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ preset: presetName }),
      });

      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }

      const result = await response.json();
      setCurrentPreset(result.current);
      pushNotification({
        type: "success",
        title: "Risk preset updated",
        message: result.message || `Risk preset set to ${presetName}`
      });
      await fetchPresets(); // Refresh to show updated state
    } catch (error: any) {
      console.error("Failed to set preset:", error);
      pushNotification({
        type: "error",
        title: "Failed to update risk preset",
        message: error.message || "Unknown error"
      });
    } finally {
      setLoading(false);
    }
  };

  if (!presetsData) {
    return (
      <div className="p-6">
        <h1 className="text-2xl font-bold mb-4">Risk Presets</h1>
        <p className="text-gray-600">Loading...</p>
      </div>
    );
  }

  const presetOrder = ["conservative", "moderate", "aggressive"];
  const orderedPresets = presetOrder
    .filter(name => presetsData.presets[name])
    .map(name => ({ name, config: presetsData.presets[name] }));

  return (
    <div className="p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold mb-2">Risk Presets</h1>
        <p className="text-gray-600">
          Choose your risk management profile. Settings apply to position sizing,
          stop loss/take profit placement, and drawdown limits.
        </p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {orderedPresets.map(({ name, config }) => {
          const isActive = currentPreset === name;
          const borderColor = isActive
            ? "border-blue-500"
            : "border-gray-300";
          const bgColor = isActive ? "bg-blue-50" : "bg-white";

          return (
            <div
              key={name}
              className={`border-2 ${borderColor} ${bgColor} rounded-lg p-6 shadow-sm transition-all`}
            >
              <div className="flex items-center justify-between mb-3">
                <h2 className="text-xl font-semibold capitalize">{config.name}</h2>
                {isActive && (
                  <span className="bg-blue-500 text-white text-xs px-2 py-1 rounded">
                    ACTIVE
                  </span>
                )}
              </div>

              <p className="text-gray-600 text-sm mb-4">{config.description}</p>

              <div className="space-y-2 mb-4 text-sm">
                <div className="flex justify-between">
                  <span className="text-gray-600">Risk per trade:</span>
                  <span className="font-medium">
                    {(config.risk_per_trade_multiplier * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Stop loss:</span>
                  <span className="font-medium">
                    {(config.stop_loss_multiplier * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Take profit:</span>
                  <span className="font-medium">
                    {(config.take_profit_multiplier * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Max daily loss:</span>
                  <span className="font-medium">{config.max_daily_loss_pct}%</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Max concurrent:</span>
                  <span className="font-medium">
                    {config.max_concurrent_trades.total} trades
                  </span>
                </div>
                <div className="flex justify-between">
                  <span className="text-gray-600">Loss streak limit:</span>
                  <span className="font-medium">
                    {config.max_consecutive_losses}
                  </span>
                </div>
              </div>

              <button
                onClick={() => selectPreset(name)}
                disabled={isActive || loading}
                className={`w-full py-2 px-4 rounded font-medium transition-colors ${
                  isActive
                    ? "bg-gray-200 text-gray-500 cursor-not-allowed"
                    : "bg-blue-500 text-white hover:bg-blue-600"
                }`}
              >
                {isActive ? "Active" : loading ? "Applying..." : "Select"}
              </button>
            </div>
          );
        })}
      </div>

      <div className="mt-8 bg-yellow-50 border border-yellow-200 rounded-lg p-4">
        <h3 className="font-semibold text-yellow-800 mb-2">⚠️ Important Notes</h3>
        <ul className="text-sm text-yellow-700 space-y-1">
          <li>• Preset changes apply immediately to new trades</li>
          <li>• Existing open positions keep their original stop loss / take profit</li>
          <li>• Conservative preset reduces risk but may limit profit potential</li>
          <li>• Aggressive preset increases potential returns but also risk exposure</li>
        </ul>
      </div>
    </div>
  );
}
