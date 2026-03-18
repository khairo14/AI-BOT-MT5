import TradingModePage from "@/components/dashboard/TradingModePage";

export default function ScalpingPage() {
  return (
    <TradingModePage
      mode="scalping"
      label="Scalping"
      icon="⚡"
      symbols={["EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "BTCUSD", "USOIL", "ETHUSD"]}
      defaultSymbol="EURUSD"
      defaultTimeframe="M5"
      timeframes={["M1", "M5", "M15"]}
      strategyNames={["EMA Scalp", "BB Squeeze", "VWAP Reversion"]}
    />
  );
}
