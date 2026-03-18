"use client";
import { useEffect, useState } from "react";
import { fetchJournalStats, fetchTradeJournal } from "@/lib/api";
import type { JournalStatsResponse, JournalEntry } from "@/types";

const MODE_LABELS = { paper: "Paper / Demo", live: "Live" } as const;
const MODE_COLORS = {
  paper: "text-emerald-400",
  live:  "text-red-400",
} as const;

function StatCard({
  label,
  value,
  sub,
}: {
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 flex flex-col gap-1">
      <span className="text-xs text-gray-500 uppercase tracking-wide">{label}</span>
      <span className="text-xl font-bold text-white">{value}</span>
      {sub && <span className="text-xs text-gray-500">{sub}</span>}
    </div>
  );
}

export default function AccountPanel() {
  const [stats, setStats]     = useState<JournalStatsResponse | null>(null);
  const [entries, setEntries] = useState<JournalEntry[]>([]);
  const [account, setAccount] = useState<"paper" | "live" | "all">("all");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([fetchJournalStats(), fetchTradeJournal(account, undefined, 20)])
      .then(([s, j]) => {
        if (!cancelled) {
          setStats(s);
          setEntries(j.entries);
        }
      })
      .catch(() => {})
      .finally(() => !cancelled && setLoading(false));
    return () => { cancelled = true; };
  }, [account]);

  if (loading) {
    return <p className="text-sm text-gray-500">Loading journal…</p>;
  }

  return (
    <div className="space-y-6">
      {/* P&L comparison cards */}
      {stats && (
        <div className="grid grid-cols-2 gap-4">
          {(["paper", "live"] as const).map((acct) => {
            const s = stats[acct];
            return (
              <div key={acct} className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                <h4 className={`text-sm font-semibold uppercase tracking-wide mb-4 ${MODE_COLORS[acct]}`}>
                  {MODE_LABELS[acct]}
                </h4>
                <div className="grid grid-cols-2 gap-3">
                  <StatCard label="Trades"   value={String(s.total)} />
                  <StatCard label="Win Rate" value={`${s.win_rate}%`} sub={`${s.wins}W / ${s.losses}L`} />
                  <StatCard
                    label="P&L"
                    value={`${s.total_profit >= 0 ? "+" : ""}${s.total_profit.toFixed(2)}`}
                    sub="account currency"
                  />
                  <StatCard label="Bot Trades" value={String(s.total)} sub="journal entries" />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Recent journal trades */}
      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <div className="flex items-center justify-between mb-4">
          <h4 className="text-sm font-semibold text-gray-300 uppercase tracking-wide">
            Bot Trade Journal
          </h4>
          {/* Filter tabs */}
          <div className="flex gap-1 text-xs">
            {(["all", "paper", "live"] as const).map((a) => (
              <button
                key={a}
                onClick={() => setAccount(a)}
                className={`px-3 py-1 rounded-full transition-colors ${
                  account === a
                    ? "bg-blue-600 text-white"
                    : "bg-gray-800 text-gray-400 hover:text-white"
                }`}
              >
                {a === "all" ? "All" : a === "paper" ? "Paper" : "Live"}
              </button>
            ))}
          </div>
        </div>

        {entries.length === 0 ? (
          <p className="text-xs text-gray-600">No journal entries yet. Trades will appear here once the bot executes orders.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800">
                  <th className="text-left py-2 pr-4">Ticket</th>
                  <th className="text-left py-2 pr-4">Symbol</th>
                  <th className="text-left py-2 pr-4">Dir</th>
                  <th className="text-left py-2 pr-4">Lots</th>
                  <th className="text-left py-2 pr-4">Entry</th>
                  <th className="text-left py-2 pr-4">P&L</th>
                  <th className="text-left py-2 pr-4">Type</th>
                  <th className="text-left py-2 pr-4">Account</th>
                  <th className="text-left py-2">Event</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {entries.map((e, i) => (
                  <tr key={i} className="text-gray-300 hover:bg-gray-800/50 transition-colors">
                    <td className="py-2 pr-4 font-mono text-gray-500">#{e.ticket}</td>
                    <td className="py-2 pr-4 font-semibold">{e.symbol}</td>
                    <td className={`py-2 pr-4 font-semibold ${e.direction === "buy" ? "text-green-400" : "text-red-400"}`}>
                      {e.direction.toUpperCase()}
                    </td>
                    <td className="py-2 pr-4">{e.volume}</td>
                    <td className="py-2 pr-4 font-mono">{e.entry}</td>
                    <td className={`py-2 pr-4 font-mono ${
                      e.profit == null ? "text-gray-500" :
                      e.profit >= 0 ? "text-green-400" : "text-red-400"
                    }`}>
                      {e.profit == null ? "—" : `${e.profit >= 0 ? "+" : ""}${e.profit.toFixed(2)}`}
                    </td>
                    <td className="py-2 pr-4 text-gray-500">{e.trading_type}</td>
                    <td className="py-2 pr-4">
                      <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${
                        e.account_mode === "live"
                          ? "bg-red-900/40 text-red-300"
                          : "bg-emerald-900/40 text-emerald-300"
                      }`}>
                        {e.account_mode}
                      </span>
                    </td>
                    <td className="py-2 text-gray-500 capitalize">{e.event}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
