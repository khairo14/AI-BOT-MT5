"use client";
import type { AccountInfo } from "@/types";

interface Props {
  account: AccountInfo | null;
}

function Stat({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl px-5 py-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`mt-1 text-xl font-bold ${color ?? "text-white"}`}>{value}</p>
    </div>
  );
}

export default function AccountSummary({ account }: Props) {
  if (!account) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4 animate-pulse">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="bg-gray-900 rounded-xl h-20" />
        ))}
      </div>
    );
  }

  const pl = account.profit;
  const plColor = pl > 0 ? "text-emerald-400" : pl < 0 ? "text-red-400" : "text-white";

  return (
    <div className="space-y-3">
      {/* Account info row - NEW */}
      <div className="flex items-center justify-between bg-gray-900/50 rounded-lg px-4 py-2 border border-gray-800">
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500 uppercase tracking-wide">Account</span>
          <span className="font-mono font-bold text-white">{account.login}</span>
          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
            account.mode === "paper" 
              ? "bg-emerald-900/50 text-emerald-400" 
              : "bg-red-900/50 text-red-400"
          }`}>
            {account.mode === "paper" ? "DEMO" : "LIVE"}
          </span>
          {account.server && (
            <span className="text-xs text-gray-500">{account.server}</span>
          )}
        </div>
        <div className="text-xs text-gray-500">
          Leverage: 1:{account.leverage}
        </div>
      </div>

      {/* Stats grid */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Balance" value={`$${account.balance.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} />
        <Stat label="Equity" value={`$${account.equity.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} />
        <Stat
          label="Open P&L"
          value={`${pl >= 0 ? "+" : ""}$${pl.toFixed(2)}`}
          color={plColor}
        />
        <Stat label="Free Margin" value={`$${account.free_margin.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} />
      </div>
    </div>
  );
}