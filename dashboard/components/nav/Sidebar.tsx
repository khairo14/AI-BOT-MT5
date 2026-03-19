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
  { href: "/notifications", label: "Notifications", icon: "🔔" },
  { href: "/guide", label: "Trading Guide", icon: "📖" },
];

export default function Sidebar() {
  const pathname = usePathname();
  const { account, wsConnected, notifications } = useBotStore();
  const unreadCount = notifications.length;

  const [switchTarget, setSwitchTarget] = useState<AccountMode | null>(null);

  const handleModeToggle = () => {
    if (!account) return;
    const next: AccountMode = account.mode === "paper" ? "live" : "paper";
    setSwitchTarget(next);
  };

  return (
    <>
    <aside className="w-56 min-h-screen bg-gray-950 border-r border-gray-800 flex flex-col">
      {/* Logo */}
      <div className="px-4 py-5 border-b border-gray-800">
        <h1 className="text-lg font-bold text-white tracking-tight">EVOTRADE-AI</h1>
        <p className="text-xs text-gray-500 mt-0.5">XM Trading Dashboard</p>
      </div>

      {/* Nav links */}
      <nav className="flex-1 px-2 py-4 space-y-1">
        {NAV.map(({ href, label, icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-blue-600 text-white"
                  : "text-gray-400 hover:text-white hover:bg-gray-800"
              }`}
            >
              <span className="text-base">{icon}</span>
              <span className="flex-1">{label}</span>
              {href === "/notifications" && unreadCount > 0 && (
                <span className="text-xs bg-blue-600 text-white rounded-full px-1.5 py-0.5 leading-none">
                  {unreadCount}
                </span>
              )}
            </Link>
          );
        })}
      </nav>

      {/* Account status */}
      <div className="px-3 py-4 border-t border-gray-800 space-y-3">
        {/* Live / Demo toggle */}
        <button
          onClick={handleModeToggle}
          className={`w-full text-xs px-3 py-1.5 rounded-full font-semibold transition-colors ${
            account?.mode === "live"
              ? "bg-red-600 hover:bg-red-700 text-white"
              : "bg-emerald-700 hover:bg-emerald-600 text-white"
          }`}
        >
          {account?.mode === "live" ? "🔴 LIVE" : "🟢 PAPER / DEMO"}
        </button>

        {/* WS indicator */}
        <div className="flex items-center gap-2 text-xs text-gray-500">
          <span
            className={`w-2 h-2 rounded-full ${
              wsConnected ? "bg-green-400 animate-pulse" : "bg-gray-600"
            }`}
          />
          {wsConnected ? "Live feed active" : "Reconnecting…"}
        </div>

        {/* Balance */}
        {account && (
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
