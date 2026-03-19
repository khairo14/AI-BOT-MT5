import TradingModePage from "@/components/dashboard/TradingModePage";

export default function ScalpingPage() {
  return (
    <TradingModePage
      mode="scalping"
      label="Scalping"
      icon="⚡"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"] },
        { label: "Forex Minors",  symbols: ["EURJPY"] },
        { label: "Indices",       symbols: ["US100Cash", "US30Cash"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="M5"
      timeframes={["M1", "M5"]}
      strategyNames={["EMA Scalp", "BB Squeeze", "VWAP Reversion"]}
    />
  );
}
