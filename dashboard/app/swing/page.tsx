import TradingModePage from "@/components/dashboard/TradingModePage";

export default function SwingPage() {
  return (
    <TradingModePage
      mode="swing"
      label="Swing Trading"
      icon="〰"
      defaultSymbol="EURUSD"
      defaultTimeframe="D1"
      timeframes={["H4", "D1", "W1"]}
      strategyNames={["EMA Trend Rider", "Fibonacci RSI", "Weekly Breakout"]}
    />
  );
}
