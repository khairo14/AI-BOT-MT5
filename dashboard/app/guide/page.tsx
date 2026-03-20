import Link from "next/link";

const SECTIONS = [
  {
    id: "modes",
    title: "Trading Modes",
    content: [
      {
        heading: "Scalping (M1–M5)",
        body: `Scalping targets small, rapid price movements with very tight stop-losses (5–15 pips) and short hold times (seconds to minutes). 
Ideal session: London open (08:00–10:00 GMT) and NY open (13:30–15:00 GMT).
Risk per trade: 0.5% of balance. Max 3 concurrent trades.
Strategies: EMA Scalp, BB Squeeze, VWAP Reversion.
Execution: MQL5 EA on MT5 (sub-millisecond latency).`,
      },
      {
        heading: "Day Trading (M15–H1)",
        body: `Day trading captures intraday trends and breakouts. SL 20–50 pips, TP 40–100 pips. All positions close before end of day.
Ideal session: London + NY overlap (13:00–17:00 GMT).
Risk per trade: 1% of balance. Max 5 concurrent trades.
Strategies: MACD EMA Trend, S/R Breakout, RSI Divergence.`,
      },
      {
        heading: "Swing Trading (D1–W1)",
        body: `Swing trading holds positions for days to weeks, targeting large structural moves. SL 100–300 pips, TP 200–600 pips.
No session restriction — entries based on daily/weekly bar closes.
Risk per trade: 1.5% of balance. Max 2 concurrent trades.
Strategies: EMA Trend Rider, Fibonacci RSI, Weekly Breakout.`,
      },
    ],
  },
  {
    id: "execution",
    title: "Execution Modes",
    content: [
      {
        heading: "Manual (default)",
        body: `Strategies generate signals that appear in the Signal Queue. You review each signal, check the entry/SL/TP levels, and click Approve or Reject before any order is placed. Recommended while live trading.`,
      },
      {
        heading: "Auto",
        body: `Approved automatically by the bot — orders are placed immediately when a signal fires. Use only after extensive paper trading validation. Enable per mode from the Config panel.`,
      },
    ],
  },
  {
    id: "risk",
    title: "Risk Management",
    content: [
      {
        heading: "Position Sizing",
        body: `Lot size = (Balance × Risk%) ÷ (SL in pips × Pip value). The bot calculates this automatically for each order. You never exceed your configured risk per trade.`,
      },
      {
        heading: "Circuit Breakers",
        body: `• Daily drawdown limit: 5% — halts all trading when hit.
• Weekly drawdown limit: 10% — halts all trading until Monday.
• 3 consecutive losses on same mode: 30-minute cooling-off pause.
• Max concurrent trades per mode enforced at order placement.`,
      },
      {
        heading: "Stop-Loss Discipline",
        body: `Never trade without a SL. The bot enforces a minimum R:R ratio of 1.5:1 (TP must be at least 1.5× your SL distance). Trailing stops are available for day and swing trades — activate via Modify Position.`,
      },
    ],
  },
  {
    id: "trailing",
    title: "Trailing Stop Guide",
    content: [
      {
        heading: "When to Use",
        body: `Use trailing stops on trades showing strong momentum (>50% of TP hit). Move SL to breakeven first, then let the trail lock in profits.`,
      },
      {
        heading: "Trailing Stop Eligibility",
        body: `MT5 supports server-side trailing stops. Set via:
• Positions tab → hover ticket → Modify
• Enter new SL (e.g., current price - 20 pips for a buy)
• Re-check every H4 for swing, H1 for day, M15 for scalp.
Note: XM does not guarantee trailing execution at exact level during gaps or low liquidity.`,
      },
    ],
  },
  {
    id: "symbols",
    title: "Symbol Reference",
    content: [
      {
        heading: "Scalping Symbols (7)",
        body: `EURUSD, GBPUSD, USDJPY, USDCHF, EURJPY, US100Cash, US30Cash
Tightest spreads, highest intraday liquidity. Session: London (07–11 UTC) + New York (13–17 UTC).`,
      },
      {
        heading: "Day Trading Symbols (19)",
        body: `Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, GBPJPY
Commodities: GOLD, OilCash
Indices: US100Cash, US30Cash, US500Cash, GER40Cash, UK100Cash
Crypto: BTCUSD, ETHUSD
Stocks: Tesla, Nvidia, Apple, Microsoft, Amazon`,
      },
      {
        heading: "Swing Trading Symbols (23)",
        body: `Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD
Commodities: GOLD, SILVER, OilCash, BRENTCash, NGASCash
Indices: US100Cash, US500Cash
Crypto: BTCUSD, ETHUSD, XRPUSD, SOLUSD
Stocks: Tesla, Nvidia, Google, Facebook, Netflix, AdvMicroDev`,
      },
    ],
  },
  {
    id: "paper",
    title: "Paper vs Live Trading",
    content: [
      {
        heading: "Paper Trading",
        body: `Paper mode connects to your XM Demo account (login: 168442709). All trades are real orders on XM's demo server — real prices, real spreads, real execution latency. Use this to validate strategies before going live.`,
      },
      {
        heading: "Switching to Live",
        body: `Click the PAPER/DEMO button in the sidebar to switch to live. You will be prompted to confirm. The bot will reconnect to your XM Live account. Start with reduced position sizing (e.g., 0.3% risk) for the first week.`,
      },
    ],
  },
  {
    id: "strategies",
    title: "Strategy Descriptions",
    content: [
      {
        heading: "EMA Scalp",
        body: `Trades 8/21 EMA crossovers on M5 with RSI and volume confirmation. Entries only during high-volume sessions.`,
      },
      {
        heading: "BB Squeeze",
        body: `Detects Bollinger Band squeeze (low volatility) and enters on the breakout expansion candle. Uses ATR for SL placement.`,
      },
      {
        heading: "VWAP Reversion",
        body: `Fades extreme deviations from VWAP (>2 standard deviations) when momentum stalls, targeting a return to the mean.`,
      },
      {
        heading: "MACD EMA Trend",
        body: `Rides confirmed H1/H4 trends using 50/200 EMA crossover aligned with MACD histogram direction. Respects trend only.`,
      },
      {
        heading: "S/R Breakout",
        body: `Identifies key swing highs/lows as support/resistance and trades confirmed breakouts with volume surge. Avoids false breaks using a retest entry.`,
      },
      {
        heading: "RSI Divergence",
        body: `Detects bullish/bearish RSI divergence on H4 against the prevailing trend for counter-trend corrections.`,
      },
      {
        heading: "EMA Trend Rider",
        body: `Long-term 21/50 EMA crossover on D1 with ADX > 25 trend strength filter. Rides multi-day impulse moves.`,
      },
      {
        heading: "Fibonacci RSI",
        body: `Combines daily Fibonacci retracement levels (38.2%, 50%, 61.8%) with RSI oversold/overbought signals for high-probability swing entries.`,
      },
      {
        heading: "Weekly Breakout",
        body: `Trades breakouts above/below the previous week's high/low on Monday, holding through the week with wide SL.`,
      },
    ],
  },
];

export default function GuidePage() {
  return (
    <div className="p-8 max-w-4xl space-y-10">
      {/* Header */}
      <div>
        <h2 className="text-2xl font-bold text-white">📖 Trading Reference Guide</h2>
        <p className="text-gray-500 text-sm mt-2">
          A complete in-app reference for this trading system. Jump to any section below.
        </p>
        {/* TOC */}
        <div className="mt-4 flex flex-wrap gap-2">
          {SECTIONS.map((s) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className="text-xs px-3 py-1.5 rounded-full bg-gray-800 text-gray-400 hover:text-white hover:bg-gray-700 transition-colors"
            >
              {s.title}
            </a>
          ))}
        </div>
      </div>

      {/* Sections */}
      {SECTIONS.map((section) => (
        <section key={section.id} id={section.id} className="scroll-mt-6">
          <h3 className="text-lg font-semibold text-white border-b border-gray-800 pb-2 mb-5">
            {section.title}
          </h3>
          <div className="space-y-5">
            {section.content.map((item) => (
              <div key={item.heading} className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <h4 className="font-medium text-blue-400 mb-2">{item.heading}</h4>
                <p className="text-gray-300 text-sm leading-relaxed whitespace-pre-line">
                  {item.body}
                </p>
              </div>
            ))}
          </div>
        </section>
      ))}

      {/* Footer */}
      <div className="bg-gray-900 border border-yellow-800/40 rounded-xl p-5 text-sm text-yellow-200/70">
        ⚠️ <strong>Disclaimer:</strong> This bot is for educational and personal use only. Trading CFDs involves substantial risk of loss. Past performance does not guarantee future results. Always use proper risk management and never risk capital you cannot afford to lose.
      </div>
    </div>
  );
}
