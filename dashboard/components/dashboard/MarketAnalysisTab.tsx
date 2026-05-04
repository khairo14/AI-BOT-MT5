"use client";

import { useEffect, useState, useCallback } from "react";
import { fetchRegimeStatus, fetchRiskStatus, type ScanSummary } from "@/lib/api";

// ── Types ───────────────────────────────────────────────────────────────────
type Regime = "trending_bull" | "trending_bear" | "volatile_breakout" | "quiet";

interface SymbolAnalysis {
  symbol: string;
  regime: Regime;
  category: string;
  bestScore: number | null;
  bestMode: string | null;
  tradingHoursActive: boolean;
}

interface RiskStatus {
  daily_halted: boolean;
  weekly_halted: boolean;
  consecutive_losses: Record<string, number>;
  paused_modes: Record<string, string | null>;
  day_start_balance: number;
  week_start_balance: number;
}

// ── Regime display config ───────────────────────────────────────────────────
const REGIME_CFG: Record<
  string,
  {
    label: string;
    summary: string;
    guidanceShort: string;
    icon: string;
    cardBg: string;
    cardBorder: string;
    cardText: string;
    badgeBg: string;
    badgeText: string;
    recLabel: string;
    recColor: string;
  }
> = {
  trending_bull: {
    label: "Trending Bull",
    summary: "Strong upward momentum. Long entries favoured.",
    guidanceShort: "Long Signals Active",
    icon: "↑",
    cardBg: "bg-green-500/10",
    cardBorder: "border-green-500/30",
    cardText: "text-green-400",
    badgeBg: "bg-green-500/20",
    badgeText: "text-green-300",
    recLabel: "Ready (Long)",
    recColor: "text-green-400",
  },
  trending_bear: {
    label: "Trending Bear",
    summary: "Strong downward momentum. Short entries favoured.",
    guidanceShort: "Short Signals Active",
    icon: "↓",
    cardBg: "bg-red-500/10",
    cardBorder: "border-red-500/30",
    cardText: "text-red-400",
    badgeBg: "bg-red-500/20",
    badgeText: "text-red-300",
    recLabel: "Ready (Short)",
    recColor: "text-red-400",
  },
  volatile_breakout: {
    label: "Volatile / Breakout",
    summary:
      "High volatility & erratic price action. Avoid scalping. Widen stops if trading.",
    guidanceShort: "Caution — Avoid Scalping",
    icon: "⚡",
    cardBg: "bg-yellow-500/10",
    cardBorder: "border-yellow-500/30",
    cardText: "text-yellow-400",
    badgeBg: "bg-yellow-500/20",
    badgeText: "text-yellow-300",
    recLabel: "Caution",
    recColor: "text-yellow-400",
  },
  quiet: {
    label: "Quiet / Ranging",
    summary: "Low volatility. No clear direction. Wait for a breakout setup.",
    guidanceShort: "Wait for Breakout",
    icon: "—",
    cardBg: "bg-gray-800/50",
    cardBorder: "border-gray-700",
    cardText: "text-gray-400",
    badgeBg: "bg-gray-700",
    badgeText: "text-gray-400",
    recLabel: "Wait",
    recColor: "text-gray-500",
  },
};

const REGIME_ORDER: Regime[] = [
  "trending_bull",
  "trending_bear",
  "volatile_breakout",
  "quiet",
];

const MODE_LABELS: Record<string, string> = {
  scalping: "Scalp",
  day_trading: "Day",
  swing: "Swing",
};

const MAX_CONSECUTIVE_LOSSES = 7;

// ── Helpers ─────────────────────────────────────────────────────────────────
function scoreColor(score: number | null): string {
  if (score === null) return "text-gray-600";
  if (score >= 70) return "text-green-400";
  if (score >= 50) return "text-yellow-400";
  return "text-red-400";
}

function deriveCategory(sym: string, scanCategory?: string): string {
  if (scanCategory) return scanCategory;
  
  const u = sym.toUpperCase();
  
  // Commodities — check BEFORE indices since BRENTCash contains "CASH"
  if (["GOLD", "SILVER", "OIL", "BRENT", "NGAS", "XAU", "XAG"].some((k) => u.includes(k)))
    return "commodities";
    
  // Indices (exclude commodities first)
  if (["US100", "US30", "US500", "UK100", "GER40", "EU50", "FRA40", "JP225", "AUS200"].some((k) => u.includes(k)))
    return "indices";
  
  // Crypto
  if (["BTC", "ETH", "XRP", "SOL", "BCH", "XLM", "DOGE", "ADA", "DOT", "LTC"].some((c) => u.includes(c)))
    return "crypto";
  
  // Forex
  const FX = ["USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD", "SGD", "HKD", "NOK", "SEK", "MXN", "TRY", "ZAR"];
  if (FX.some((c) => u.startsWith(c)) && FX.some((c) => u.endsWith(c)))
    return "forex";
  
  // Futures (contains hyphen)
  if (u.includes("-")) return "futures";
  
  return "stocks";
}

function buildGuidance(
  regime: Regime,
  score: number | null
): { text: string; color: string } {
  if (regime === "trending_bull") {
    if (score !== null && score >= 65)
      return { text: "✅ Ready — Long", color: "text-green-400" };
    if (score !== null && score >= 45)
      return { text: "👁 Monitor — Build Entry", color: "text-yellow-400" };
    return { text: "⏳ Low Score", color: "text-gray-500" };
  }
  if (regime === "trending_bear") {
    if (score !== null && score >= 65)
      return { text: "✅ Ready — Short", color: "text-red-400" };
    if (score !== null && score >= 45)
      return { text: "👁 Monitor — Build Entry", color: "text-yellow-400" };
    return { text: "⏳ Low Score", color: "text-gray-500" };
  }
  if (regime === "volatile_breakout")
    return { text: "⚡ Caution — Widen Stops", color: "text-yellow-400" };
  return { text: "⏳ Wait for Trend", color: "text-gray-500" };
}

// ── Component ───────────────────────────────────────────────────────────────
export default function MarketAnalysisTab({
  scanData,
}: {
  scanData: ScanSummary | null;
}) {
  const [regimes, setRegimes] = useState<Record<string, string>>({});
  const [riskStatus, setRiskStatus] = useState<RiskStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [selectedCategory, setSelectedCategory] = useState("all");
  const [expandedRegimes, setExpandedRegimes] = useState<Set<string>>(
    new Set(REGIME_ORDER)
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [regimeRes, riskRes] = await Promise.all([
        fetchRegimeStatus(),
        fetchRiskStatus(),
      ]);
      setRegimes(regimeRes.regimes ?? {});
      setRiskStatus(riskRes);
      setLastUpdated(new Date());
    } catch (err) {
      console.error("MarketAnalysis load error:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Build score map from scan results: symbol → best scan entry across modes
  const scoreMap = new Map<
    string,
    { score: number; category: string; mode: string; active: boolean }
  >();
  if (scanData) {
    for (const [mode, results] of Object.entries(scanData.trading_types)) {
      for (const r of results) {
        const cleanSymbol = r.symbol.replace(/#/g, '');
        const existing = scoreMap.get(cleanSymbol);
        if (!existing || r.composite_score > existing.score) {
          scoreMap.set(cleanSymbol, {
            score: r.composite_score,
            category: r.category,
            mode,
            active: r.trading_hours_active,
          });
        }
      }
    }
  }

  // Build unified symbol list
  const symbols: SymbolAnalysis[] = Object.entries(regimes).map(
    ([sym, regime]) => {
      const clean = sym.replace(/#/g, '');
      const scan = scoreMap.get(clean);
      return {
        symbol: sym,
        regime: regime as Regime,
        category: scan?.category ?? deriveCategory(clean),
        bestScore: scan?.score ?? null,
        bestMode: scan?.mode ?? null,
        tradingHoursActive: scan?.active ?? true,
      };
    }
  );

  // Distribution counts
  const counts = symbols.reduce(
    (acc, s) => {
      acc[s.regime] = (acc[s.regime] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>
  );

  // Category filter options
  const categories = [
    "all",
    ...Array.from(new Set(symbols.map((s) => s.category))).sort(),
  ];

  // Group by regime, filtered by category, sorted by score desc
  const grouped = REGIME_ORDER.reduce(
    (acc, r) => {
      acc[r] = symbols
        .filter(
          (s) =>
            s.regime === r &&
            (selectedCategory === "all" || s.category === selectedCategory)
        )
        .sort((a, b) => (b.bestScore ?? -1) - (a.bestScore ?? -1));
      return acc;
    },
    {} as Record<string, SymbolAnalysis[]>
  );

  // Drawdown %
  function drawdownPct(start: number, current: number): number {
    if (!start || start <= 0) return 0;
    return ((start - current) / start) * 100;
  }

  const toggleRegime = (r: string) => {
    setExpandedRegimes((prev) => {
      const next = new Set(prev);
      if (next.has(r)) next.delete(r);
      else next.add(r);
      return next;
    });
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24 text-gray-500">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-500 mr-3" />
        Loading market analysis…
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Header row */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-gray-500">
          Regime classification across{" "}
          <span className="text-gray-300 font-medium">{symbols.length}</span>{" "}
          symbols.
          {lastUpdated && (
            <span className="ml-2 text-gray-600">
              Updated {lastUpdated.toLocaleTimeString()}
            </span>
          )}
        </p>
        <button
          onClick={load}
          className="px-3 py-1.5 bg-blue-600/20 hover:bg-blue-600/30 border border-blue-500/30 text-blue-400 rounded-lg text-sm transition-colors"
        >
          ↺ Refresh
        </button>
      </div>

      {/* ── Regime Overview Cards ────────────────────────────────────────── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
        {REGIME_ORDER.map((r) => {
          const cfg = REGIME_CFG[r];
          const cnt = counts[r] ?? 0;
          return (
            <button
              key={r}
              onClick={() => setExpandedRegimes((p) => new Set([...p, r]))}
              className={`${cfg.cardBg} border ${cfg.cardBorder} rounded-xl p-4 text-left hover:opacity-90 transition-opacity`}
            >
              <div className="flex items-center justify-between mb-2">
                <span className={`text-xl font-bold ${cfg.cardText}`}>
                  {cfg.icon}
                </span>
                <span
                  className={`text-3xl font-bold font-mono ${cfg.cardText}`}
                >
                  {cnt}
                </span>
              </div>
              <div className={`text-sm font-semibold ${cfg.cardText}`}>
                {cfg.label}
              </div>
              <div className="text-xs text-gray-500 mt-1">
                {cfg.guidanceShort}
              </div>
            </button>
          );
        })}
      </div>

      {/* ── Risk Guard Rails ─────────────────────────────────────────────── */}
      {riskStatus && (
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-widest mb-3">
            Risk Guard Rails
          </h3>

          {(riskStatus.daily_halted || riskStatus.weekly_halted) && (
            <div className="mb-3 p-2 bg-red-500/10 border border-red-500/30 rounded-lg text-xs text-red-400">
              {riskStatus.daily_halted &&
                "⛔ Daily drawdown limit hit — trading halted today."}
              {riskStatus.weekly_halted &&
                " ⛔ Weekly drawdown limit hit — trading halted this week."}
            </div>
          )}

          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            {(["scalping", "day_trading", "swing"] as const).map((mode) => {
              const losses = riskStatus.consecutive_losses?.[mode] ?? 0;
              const paused = riskStatus.paused_modes?.[mode];
              const pct = Math.min(
                (losses / MAX_CONSECUTIVE_LOSSES) * 100,
                100
              );
              const barColor =
                losses >= 6
                  ? "bg-red-500"
                  : losses >= 4
                  ? "bg-yellow-500"
                  : "bg-green-500";
              const modeLabel = {
                scalping: "Scalping",
                day_trading: "Day Trading",
                swing: "Swing",
              }[mode];
              return (
                <div key={mode}>
                  <div className="flex items-center justify-between text-xs text-gray-400 mb-1.5">
                    <span className="font-medium">{modeLabel}</span>
                    <span className="font-mono">
                      {losses}/{MAX_CONSECUTIVE_LOSSES} losses
                      {paused && (
                        <span className="ml-2 text-red-400">⏸ Paused</span>
                      )}
                    </span>
                  </div>
                  <div className="h-2 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${barColor}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>

          {/* Drawdown snapshot */}
          {riskStatus.day_start_balance > 0 && (
            <div className="mt-3 pt-3 border-t border-gray-800 flex gap-6 text-xs text-gray-500">
              <span>
                Day P&L:{" "}
                <span
                  className={
                    drawdownPct(
                      riskStatus.day_start_balance,
                      riskStatus.day_start_balance
                    ) > 0
                      ? "text-red-400"
                      : "text-gray-400"
                  }
                >
                  from ${riskStatus.day_start_balance.toLocaleString()}
                </span>
              </span>
              <span>
                Week start:{" "}
                <span className="text-gray-400">
                  ${riskStatus.week_start_balance.toLocaleString()}
                </span>
              </span>
            </div>
          )}
        </div>
      )}

      {/* ── Category Filter ──────────────────────────────────────────────── */}
      <div className="flex gap-2 flex-wrap items-center">
        <span className="text-xs text-gray-500 mr-1">Filter:</span>
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => setSelectedCategory(cat)}
            className={`px-3 py-1 rounded-full text-xs font-medium capitalize transition-colors ${
              selectedCategory === cat
                ? "bg-blue-600 text-white"
                : "bg-gray-800 text-gray-400 hover:bg-gray-700"
            }`}
          >
            {cat === "all" ? "All Categories" : cat}
          </button>
        ))}
      </div>

      {/* ── Grouped Symbol Tables ────────────────────────────────────────── */}
      {REGIME_ORDER.map((r) => {
        const cfg = REGIME_CFG[r];
        const items = grouped[r];
        if (!items || items.length === 0) return null;
        const isExpanded = expandedRegimes.has(r);

        return (
          <div
            key={r}
            className={`border ${cfg.cardBorder} rounded-xl overflow-hidden`}
          >
            {/* Section header — clickable to collapse */}
            <button
              className={`w-full flex items-center justify-between px-5 py-3 ${cfg.cardBg} hover:opacity-80 transition-opacity`}
              onClick={() => toggleRegime(r)}
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className={`text-lg font-bold ${cfg.cardText} shrink-0`}>
                  {cfg.icon}
                </span>
                <span className={`font-semibold text-sm ${cfg.cardText} shrink-0`}>
                  {cfg.label}
                </span>
                <span className="text-xs text-gray-600 bg-gray-800 px-2 py-0.5 rounded-full shrink-0">
                  {items.length}
                </span>
                <span className="text-xs text-gray-500 italic truncate hidden sm:block">
                  {cfg.summary}
                </span>
              </div>
              <span className="text-gray-600 text-xs ml-3 shrink-0">
                {isExpanded ? "▲ collapse" : "▼ expand"}
              </span>
            </button>

            {isExpanded && (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="bg-gray-800/50 text-xs uppercase tracking-wide text-gray-500">
                      <th className="text-left py-2 px-4">Symbol</th>
                      <th className="text-left py-2 px-4">Category</th>
                      <th className="text-left py-2 px-4">Score</th>
                      <th className="text-left py-2 px-4">Detected By</th>
                      <th className="text-left py-2 px-4">Hours</th>
                      <th className="text-left py-2 px-4">Guidance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((item) => {
                      const guidance = buildGuidance(item.regime, item.bestScore);
                      return (
                        <tr
                          key={item.symbol}
                          className="border-t border-gray-800/60 hover:bg-gray-800/30 transition-colors"
                        >
                          <td className="py-2 px-4 font-mono font-bold text-gray-100 whitespace-nowrap">
                            {item.symbol}
                          </td>
                          <td className="py-2 px-4">
                            <span className="px-2 py-0.5 bg-gray-800 text-gray-400 rounded text-xs capitalize">
                              {item.category}
                            </span>
                          </td>
                          <td className="py-2 px-4 font-mono">
                            {item.bestScore !== null ? (
                              <span className={scoreColor(item.bestScore)}>
                                {item.bestScore.toFixed(1)}
                              </span>
                            ) : (
                              <span className="text-gray-600">—</span>
                            )}
                          </td>
                          <td className="py-2 px-4 text-xs text-gray-500">
                            {item.bestMode
                              ? MODE_LABELS[item.bestMode] ?? item.bestMode
                              : "—"}
                          </td>
                          <td className="py-2 px-4 text-xs">
                            {item.tradingHoursActive ? (
                              <span className="text-green-400">● Active</span>
                            ) : (
                              <span className="text-gray-600">○ Closed</span>
                            )}
                          </td>
                          <td
                            className={`py-2 px-4 text-xs font-medium whitespace-nowrap ${guidance.color}`}
                          >
                            {guidance.text}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        );
      })}

      {/* Empty state */}
      {symbols.length === 0 && !loading && (
        <div className="text-center py-16 text-gray-600">
          <p className="text-3xl mb-2">📊</p>
          <p>No regime data available. Is the bot running?</p>
        </div>
      )}
    </div>
  );
}
