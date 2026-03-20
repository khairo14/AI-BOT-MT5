import TradingModePage from "@/components/dashboard/TradingModePage";

export default function DayTradingPage() {
  return (
    <TradingModePage
      mode="day_trading"
      label="Day Trading"
      icon="☀"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"] },
        { label: "Forex Minors",  symbols: ["GBPJPY"] },
        { label: "Commodities",   symbols: ["GOLD", "OILCash"] },
        { label: "Indices",       symbols: ["US100Cash", "US30Cash", "US500Cash", "GER40Cash", "UK100Cash"] },
        { label: "Crypto",        symbols: ["BTCUSD", "ETHUSD"] },
        { label: "Stocks",        symbols: ["Tesla", "Nvidia", "Apple", "Microsoft", "Amazon"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="H1"
      timeframes={["M15", "M30", "H1"]}
      strategyNames={["MACD EMA Trend", "S/R Breakout", "RSI Divergence"]}
    />
  );
}
