"use client";
import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useBotStore } from "@/lib/store";
import TradeSwitchModal from "@/components/account/TradeSwitchModal";
import type { AccountMode } from "@/types";

// ── Market session definitions ─────────────────────────────────────────────
// All times in UTC hours [open, close]. Sessions that cross midnight use two ranges.
const SESSIONS = [
  {
    name: "Forex",
    icon: "💱",
    // Forex is open Sun 22:00 – Fri 22:00 UTC (continuous weekday)
    // We approximate as Mon-Fri 00:00–24:00; closed Sat + Sun before 22:00
    type: "forex",
  },
  {
    name: "US Stocks",
    icon: "🗽",
    // NYSE/NASDAQ: Mon-Fri 13:30–20:00 UTC
    openH: 13, openM: 30, closeH: 20, closeM: 0,
    days: [1, 2, 3, 4, 5], // Mon-Fri
    type: "equity",
  },
  {
    name: "Crypto",
    icon: "₿",
    // 24/7
    type: "crypto",
  },
  {
    name: "Commodities",
    icon: "🛢",
    // Oil / Gold (CME): Mon 00:00 – Fri 23:00 UTC (approx)
    openH: 0, openM: 0, closeH: 23, closeM: 0,
    days: [1, 2, 3, 4, 5],
    type: "equity",
  },
] as const;

function getSessionStatus(now: Date): { open: boolean; nextMs: number } {
  return { open: false, nextMs: 0 }; // placeholder — per-session logic below
}

function getForexStatus(now: Date): { open: boolean; label: string } {
  const day = now.getUTCDay(); // 0=Sun, 6=Sat
  const h = now.getUTCHours(), m = now.getUTCMinutes();
  const mins = h * 60 + m;
  // Open: Mon 00:00 – Fri 22:00 UTC; also Sun >= 22:00
  if (day === 6) return { open: false, label: "Opens Sun 22:00 UTC" };
  if (day === 0 && mins < 22 * 60) {
    const diff = 22 * 60 - mins;
    return { open: false, label: `Opens in ${Math.floor(diff / 60)}h ${diff % 60}m` };
  }
  if (day === 5 && mins >= 22 * 60) {
    const diff = (24 * 60 - mins) + (24 * 60) + 22 * 60; // to Sun 22:00
    return { open: false, label: `Opens in ${Math.floor(diff / 60)}h ${diff % 60}m` };
  }
  return { open: true, label: "Open" };
}

function getCryptoStatus(): { open: boolean; label: string } {
  return { open: true, label: "Open 24/7" };
}

function getEquityStatus(
  now: Date,
  openH: number, openM: number, closeH: number, closeM: number,
  days: readonly number[]
): { open: boolean; label: string } {
  const day = now.getUTCDay();
  const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
  const openMins = openH * 60 + openM;
  const closeMins = closeH * 60 + closeM;

  if (!days.includes(day as 1|2|3|4|5)) {
    // Find next open day
    let daysAhead = 1;
    while (!days.includes(((day + daysAhead) % 7) as 1|2|3|4|5)) daysAhead++;
    const minsUntil = daysAhead * 24 * 60 - mins + openMins;
    return { open: false, label: `Opens in ${Math.floor(minsUntil / 60)}h ${minsUntil % 60}m` };
  }
  if (mins < openMins) {
    const diff = openMins - mins;
    return { open: false, label: `Opens in ${Math.floor(diff / 60)}h ${diff % 60}m` };
  }
  if (mins >= closeMins) {
    // Next open day
    let daysAhead = 1;
    while (!days.includes(((day + daysAhead) % 7) as 1|2|3|4|5)) daysAhead++;
    const minsUntil = daysAhead * 24 * 60 - mins + openMins;
    return { open: false, label: `Opens in ${Math.floor(minsUntil / 60)}h ${minsUntil % 60}m` };
  }
  const diffClose = closeMins - mins;
  return { open: true, label: `Closes in ${Math.floor(diffClose / 60)}h ${diffClose % 60}m` };
}

function ClockAndMarkets() {
  const [now, setNow] = useState<Date | null>(null);
  useEffect(() => {
    setNow(new Date());
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  if (!now) return null;

  const utcTime = now.toUTCString().slice(17, 25);
  const localTime = now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const localTZ = Intl.DateTimeFormat().resolvedOptions().timeZone.split("/").pop()?.replace("_", " ") ?? "";

  const forex = getForexStatus(now);
  const crypto = getCryptoStatus();
  const usStocks = getEquityStatus(now, 13, 30, 20, 0, [1,2,3,4,5]);
  const commodities = getEquityStatus(now, 0, 0, 23, 0, [1,2,3,4,5]);

  const markets = [
    { name: "Forex", icon: "💱", ...forex },
    { name: "Crypto", icon: "₿", ...crypto },
    { name: "US Stocks", icon: "🗽", ...usStocks },
    { name: "Commodities", icon: "🛢", ...commodities },
  ];

  return (
    <div className="px-2 pb-3 space-y-2">
      {/* Clock */}
      <div className="bg-gray-900 rounded-lg p-2 space-y-0.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500">Local ({localTZ})</span>
          <span className="font-mono text-white">{localTime}</span>
        </div>
        <div className="flex items-center justify-between text-xs">
          <span className="text-gray-500">UTC</span>
          <span className="font-mono text-blue-400">{utcTime}</span>
        </div>
      </div>
      {/* Market sessions */}
      <div className="space-y-1">
        {markets.map((m) => (
          <div key={m.name} className="flex items-center gap-1.5 text-xs">
            <span>{m.icon}</span>
            <span className={`font-medium flex-1 whitespace-nowrap ${m.open ? "text-gray-200" : "text-gray-500"}`}>
              {m.name}
            </span>
            <span className={`text-[10px] font-semibold whitespace-nowrap ${m.open ? "text-green-400" : "text-gray-500"}`}>
              {m.open ? "● Open" : m.label}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

const NAV_SECTIONS = [
  {
    title: "Trading",
    items: [
      { href: "/", label: "Overview", icon: "⊞" },
      { href: "/scalping", label: "Scalping", icon: "⚡" },
      { href: "/day-trading", label: "Day Trading", icon: "☀" },
      { href: "/swing", label: "Swing", icon: "〰" },
    ],
  },
  {
    title: "Tools",
    items: [
      { href: "/scanner", label: "Market Scanner", icon: "🔍" },
      { href: "/ml", label: "AI / ML Brain", icon: "🧠" },
      { href: "/backtest", label: "Backtest", icon: "📈" },
    ],
  },
  {
    title: "Reports",
    items: [
      { href: "/analytics", label: "Performance", icon: "📊" },
      { href: "/profitability", label: "Profitability", icon: "💰" },
    ],
  },
  {
    title: "System",
    items: [
      { href: "/notifications", label: "Notifications", icon: "🔔" },
      { href: "/risk-presets", label: "Risk Presets", icon: "⚖️" },
      { href: "/guide", label: "Trading Guide", icon: "📖" },
      { href: "/settings", label: "Settings", icon: "⚙️" },
    ],
  },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { account, wsConnected, notifications } = useBotStore();
  const unreadCount = notifications.length;

  const [collapsed, setCollapsed] = useState(false);
  const [switchTarget, setSwitchTarget] = useState<AccountMode | null>(null);

  const handleModeToggle = () => {
    if (!account) return;
    const next: AccountMode = account.mode === "paper" ? "live" : "paper";
    setSwitchTarget(next);
  };

  return (
    <>
    <aside
      className="min-h-screen bg-gray-950 border-r border-gray-800 flex flex-col transition-all duration-300 overflow-hidden"
      style={{ width: collapsed ? "56px" : "224px" }}
    >
      {/* Logo + collapse toggle */}
      <div className="px-3 py-5 border-b border-gray-800 flex items-center justify-between gap-2 min-h-15">
        {!collapsed && (
          <div className="overflow-hidden">
            <h1 className="text-lg font-bold text-white tracking-tight whitespace-nowrap">EVOTRADE-AI</h1>
            <p className="text-xs text-gray-500 mt-0.5 whitespace-nowrap">XM Trading Dashboard</p>
          </div>
        )}
        <button
          onClick={() => setCollapsed(v => !v)}
          className="ml-auto shrink-0 w-7 h-7 flex items-center justify-center rounded-md text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? "»" : "«"}
        </button>
      </div>

      {/* Nav links */}
      <nav className="flex-1 px-2 py-4 space-y-4 overflow-y-auto">
        {NAV_SECTIONS.map((section) => (
          <div key={section.title}>
            {!collapsed && (
              <div className="px-2 mb-2 text-xs font-semibold text-gray-500 uppercase tracking-wider">
                {section.title}
              </div>
            )}
            <div className="space-y-1">
              {section.items.map(({ href, label, icon }) => {
                const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
                return (
                  <Link
                    key={href}
                    href={href}
                    title={collapsed ? label : undefined}
                    className={`flex items-center gap-3 px-2 py-2 rounded-lg text-sm transition-colors ${
                      active
                        ? "bg-blue-600 text-white"
                        : "text-gray-400 hover:text-white hover:bg-gray-800"
                    }`}
                  >
                    <span className="text-base shrink-0">{icon}</span>
                    {!collapsed && (
                      <>
                        <span className="flex-1 whitespace-nowrap">{label}</span>
                        {href === "/notifications" && unreadCount > 0 && (
                          <span className="text-xs bg-blue-600 text-white rounded-full px-1.5 py-0.5 leading-none">
                            {unreadCount}
                          </span>
                        )}
                      </>
                    )}
                    {collapsed && href === "/notifications" && unreadCount > 0 && (
                      <span className="absolute ml-3 -mt-3 text-[9px] bg-blue-600 text-white rounded-full px-1 leading-none">
                        {unreadCount}
                      </span>
                    )}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* Clock + market sessions */}
      {!collapsed && <ClockAndMarkets />}

      {/* Account status */}
      <div className="px-2 py-4 border-t border-gray-800 space-y-3">
        {/* Live / Demo toggle */}
        <button
          onClick={handleModeToggle}
          title={account?.mode === "live" ? "LIVE" : "PAPER / DEMO"}
          className={`w-full text-xs px-2 py-1.5 rounded-full font-semibold transition-colors ${
            account?.mode === "live"
              ? "bg-red-600 hover:bg-red-700 text-white"
              : "bg-emerald-700 hover:bg-emerald-600 text-white"
          }`}
        >
          {collapsed
            ? (account?.mode === "live" ? "🔴" : "🟢")
            : (account?.mode === "live" ? "🔴 LIVE" : "🟢 PAPER / DEMO")}
        </button>

        {/* WS indicator */}
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${
              wsConnected ? "bg-green-400 animate-pulse" : "bg-gray-600"
            }`}
          />
          {!collapsed && (wsConnected ? "Live feed active" : "Reconnecting…")}
        </div>

        {/* Balance */}
        {!collapsed && account && (
          <div className="text-xs text-gray-400">
            <span className="text-gray-600">Balance </span>
            <span className="text-white font-medium">
              ${account.balance.toLocaleString()}
            </span>
          </div>
        )}
      </div>
    </aside>

    {/* Account switch confirmation modal */}
    {switchTarget && (
      <TradeSwitchModal
        targetMode={switchTarget}
        onClose={() => setSwitchTarget(null)}
      />
    )}
  </>
  );
}
