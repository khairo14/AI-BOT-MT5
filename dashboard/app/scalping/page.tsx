import TradingModePage from "@/components/dashboard/TradingModePage";

export default function ScalpingPage() {
  return (
    <TradingModePage
      mode="scalping"
      label="Scalping"
      icon="⚡"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD"] },
        { label: "Metals",        symbols: ["XAUUSD"] },
        { label: "Energy",        symbols: ["USOIL"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="M5"
      timeframes={["M1", "M5", "M15"]}
      strategyNames={["EMA Scalp", "BB Squeeze", "VWAP Reversion"]}
    />
  );
}
