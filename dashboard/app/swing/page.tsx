import TradingModePage from "@/components/dashboard/TradingModePage";

export default function SwingPage() {
  return (
    <TradingModePage
      mode="swing"
      label="Swing Trading"
      icon="〰"
      symbols={[
        "EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","NZDUSD","USDCAD",
        "GBPJPY","EURJPY","EURGBP","AUDCAD",
        "XAUUSD","XAGUSD","USOIL","UKOIL","NATGAS",
        "BTCUSD","ETHUSD",
        "US30","SPX500","NAS100","GER40","UK100",
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="D1"
      timeframes={["H4", "D1", "W1"]}
      strategyNames={["EMA Trend Rider", "Fibonacci RSI", "Weekly Breakout"]}
    />
  );
}
