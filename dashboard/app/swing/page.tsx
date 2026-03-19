import TradingModePage from "@/components/dashboard/TradingModePage";

export default function SwingPage() {
  return (
    <TradingModePage
      mode="swing"
      label="Swing Trading"
      icon="〰"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"] },
        { label: "Forex Minors",  symbols: ["GBPJPY", "EURJPY", "EURGBP", "AUDCAD"] },
        { label: "Metals",        symbols: ["XAUUSD", "XAGUSD"] },
        { label: "Energy",        symbols: ["USOIL", "UKOIL", "NATGAS"] },
        { label: "Crypto",        symbols: ["BTCUSD", "ETHUSD"] },
        { label: "Indices",       symbols: ["US30", "SPX500", "NAS100", "GER40", "UK100"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="D1"
      timeframes={["H4", "D1", "W1"]}
      strategyNames={["EMA Trend Rider", "Fibonacci RSI", "Weekly Breakout"]}
    />
  );
}
