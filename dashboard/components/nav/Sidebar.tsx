"use client";
import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useBotStore } from "@/lib/store";
import TradeSwitchModal from "@/components/account/TradeSwitchModal";
import type { AccountMode } from "@/types";

const NAV = [
  { href: "/", label: "Overview", icon: "⊞" },
  { href: "/scalping", label: "Scalping", icon: "⚡" },
  { href: "/day-trading", label: "Day Trading", icon: "☀" },
  { href: "/swing", label: "Swing", icon: "〰" },
  { href: "/ml", label: "AI / ML Brain", icon: "🧠" },
  { href: "/backtest", label: "Backtest", icon: "📈" },
  { href: "/notifications", label: "Notifications", icon: "🔔" },
  { href: "/guide", label: "Trading Guide", icon: "📖" },
  { href: "/settings", label: "Settings", icon: "⚙️" },
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
      <nav className="flex-1 px-2 py-4 space-y-1">
        {NAV.map(({ href, label, icon }) => {
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
      </nav>

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
