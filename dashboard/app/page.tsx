"use client";
import { useBotStore } from "@/lib/store";
import AccountSummary from "@/components/dashboard/AccountSummary";
import AccountPanel from "@/components/account/AccountPanel";
import PositionTable from "@/components/trading/PositionTable";
import { fetchPositions } from "@/lib/api";
import Link from "next/link";

const MODE_CARDS = [
  { href: "/scalping", label: "Scalping", icon: "Lightning", desc: "M1-M5 ultra-fast execution - 3 strategies", color: "border-yellow-600" },
  { href: "/day-trading", label: "Day Trading", icon: "Sun", desc: "H1-H4 intraday trend following - 3 strategies", color: "border-blue-600" },
  { href: "/swing", label: "Swing Trading", icon: "Wave", desc: "D1-W1 multi-day momentum - 3 strategies", color: "border-purple-600" },
];

export default function OverviewPage() {
  const { account, positions, setPositions } = useBotStore();
  const refresh = () => fetchPositions().then(setPositions).catch(() => {});

  return (
    <div className="p-4 sm:p-6 lg:p-8 space-y-6 w-full">
      <div>
        <h2 className="text-2xl font-bold text-white">Overview</h2>
        <p className="text-gray-500 text-sm mt-1">
          {account ? `${account.server} - Leverage 1:${account.leverage} - ${account.currency}` : "Connecting to MT5..."}
        </p>
      </div>

      <AccountSummary account={account} />

      <div>
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">Trading Modes</h3>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          {MODE_CARDS.map((m) => (
            <Link key={m.href} href={m.href} className={`bg-gray-900 border ${m.color} rounded-xl p-5 hover:bg-gray-800 transition-colors`}>
              <div className="text-xl font-bold mb-2 text-gray-300">{m.icon}</div>
              <h4 className="font-semibold text-white">{m.label}</h4>
              <p className="text-xs text-gray-500 mt-1">{m.desc}</p>
            </Link>
          ))}
        </div>
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">Open Positions ({positions.length})</h3>
          <button onClick={refresh} className="text-xs text-gray-500 hover:text-white transition-colors">Refresh</button>
        </div>
        <PositionTable positions={positions} onRefresh={refresh} showMode />
      </div>

      {/* Phase 9: paper vs live journal */}
      <div>
        <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">Account Performance</h3>
        <AccountPanel />
      </div>
    </div>
  );
}