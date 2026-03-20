import TradingModePage from "@/components/dashboard/TradingModePage";

export default function SwingPage() {
  return (
    <TradingModePage
      mode="swing"
      label="Swing Trading"
      icon="〰"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD"] },
        { label: "Commodities",   symbols: ["GOLD", "SILVER", "OILCash", "BRENTCash", "NGASCash"] },
        { label: "Indices",       symbols: ["US100Cash", "US500Cash"] },
        { label: "Crypto",        symbols: ["BTCUSD", "ETHUSD", "XRPUSD", "SOLUSD"] },
        { label: "Stocks",        symbols: ["Tesla", "Nvidia", "Google", "Facebook", "Netflix", "AdvMicroDev"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="D1"
      timeframes={["H4", "D1", "W1"]}
      strategyNames={["EMA Trend Rider", "Fibonacci RSI", "Weekly Breakout"]}
    />
  );
}
