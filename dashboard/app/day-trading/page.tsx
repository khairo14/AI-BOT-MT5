import TradingModePage from "@/components/dashboard/TradingModePage";

export default function DayTradingPage() {
  return (
    <TradingModePage
      mode="day_trading"
      label="Day Trading"
      icon="☀"
      defaultSymbol="EURUSD"
      defaultTimeframe="H1"
      timeframes={["M15", "M30", "H1"]}
      strategyNames={["MACD EMA Trend", "S/R Breakout", "RSI Divergence"]}
    />
  );
}
