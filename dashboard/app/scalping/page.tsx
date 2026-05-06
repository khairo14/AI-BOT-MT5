import TradingModePage from "@/components/dashboard/TradingModePage";

export default function ScalpingPage() {
  return (
    <TradingModePage
      mode="scalping"
      label="Scalping"
      icon="⚡"
      defaultSymbol="EURUSD"
      defaultTimeframe="M5"
      timeframes={["M1", "M5"]}
      strategyNames={["EMA Scalp", "BB Squeeze", "VWAP Reversion", "StochRSI Pullback"]}
    />
  );
}
