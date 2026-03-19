import TradingModePage from "@/components/dashboard/TradingModePage";

export default function DayTradingPage() {
  return (
    <TradingModePage
      mode="day_trading"
      label="Day Trading"
      icon="☀"
      symbolGroups={[
        { label: "Forex Majors",  symbols: ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"] },
        { label: "Forex Minors",  symbols: ["GBPJPY", "EURJPY", "EURGBP"] },
        { label: "Metals",        symbols: ["XAUUSD", "XAGUSD"] },
        { label: "Energy",        symbols: ["USOIL", "UKOIL"] },
        { label: "Indices",       symbols: ["US30", "SPX500", "NAS100", "GER40", "UK100", "HK50"] },
      ]}
      defaultSymbol="EURUSD"
      defaultTimeframe="H1"
      timeframes={["M30", "H1", "H4"]}
      strategyNames={["MACD EMA Trend", "S/R Breakout", "RSI Divergence"]}
    />
  );
}
