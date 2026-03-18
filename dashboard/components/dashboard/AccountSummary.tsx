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
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 animate-pulse">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="bg-gray-900 rounded-xl h-20" />
        ))}
      </div>
    );
  }

  const pl = account.profit;
  const plColor = pl > 0 ? "text-emerald-400" : pl < 0 ? "text-red-400" : "text-white";

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <Stat label="Balance" value={`$${account.balance.toLocaleString()}`} />
      <Stat label="Equity" value={`$${account.equity.toLocaleString()}`} />
      <Stat
        label="Open P&L"
        value={`${pl >= 0 ? "+" : ""}$${pl.toFixed(2)}`}
        color={plColor}
      />
      <Stat label="Free Margin" value={`$${account.free_margin.toLocaleString()}`} />
    </div>
  );
}
