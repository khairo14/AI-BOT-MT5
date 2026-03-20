"use client";
import { useEffect, useState } from "react";
import { fetchJournalStats, fetchTradeJournal, fetchPositions } from "@/lib/api";
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

type SortKey = "ticket" | "open_time" | "close_time" | "symbol" | "direction" | "volume" | "entry" | "pnl" | "trading_type";
type SortDir = "asc" | "desc";

export default function AccountPanel() {
  const [stats, setStats]         = useState<JournalStatsResponse | null>(null);
  const [entries, setEntries]     = useState<JournalEntry[]>([]);
  const [account, setAccount]     = useState<"paper" | "live" | "all">("all");
  const [loading, setLoading]     = useState(true);
  const [sortKey, setSortKey]     = useState<SortKey>("open_time");
  const [sortDir, setSortDir]     = useState<SortDir>("desc");
  // ticket → current unrealized profit for open positions
  const [liveProfit, setLiveProfit] = useState<Record<number, number>>({});

  useEffect(() => {
    let cancelled = false;

    const doFetch = (initial = false) => {
      Promise.all([
        fetchJournalStats(),
        fetchTradeJournal(account, undefined, 20),
        fetchPositions(),
      ])
        .then(([s, j, positions]) => {
          if (cancelled) return;
          setStats(s);
          setEntries(j.entries);
          const map: Record<number, number> = {};
          for (const p of positions) map[p.ticket] = p.profit;
          setLiveProfit(map);
        })
        .catch(() => {})
        .finally(() => { if (initial && !cancelled) setLoading(false); });
    };

    doFetch(true);
    const id = setInterval(() => doFetch(false), 30_000);
    return () => { cancelled = true; clearInterval(id); };
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
        ) : (() => {
          // Merge open + close rows into one row per ticket.
          // close entry takes priority (has profit); open entry fills in times.
          const merged = new Map<number, JournalEntry & { open_entry?: JournalEntry }>();
          for (const e of [...entries].reverse()) {
            const existing = merged.get(e.ticket);
            if (!existing) {
              merged.set(e.ticket, { ...e });
            } else if (e.event === "close") {
              merged.set(e.ticket, { ...e, open_time: existing.open_time ?? e.open_time });
            } else if (existing.event === "close") {
              // keep close, just fill open_time if missing
              if (!existing.open_time) existing.open_time = e.open_time;
            }
          }
          const fmtTime = (iso: string | null | undefined) =>
            iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

          const getPnl = (e: JournalEntry) =>
            e.profit ?? (!( e.event === "close") ? (liveProfit[e.ticket] ?? null) : null);

          const rows = Array.from(merged.values()).sort((a, b) => {
            let av: string | number | null = null;
            let bv: string | number | null = null;
            if (sortKey === "ticket")       { av = a.ticket;     bv = b.ticket; }
            else if (sortKey === "open_time")  { av = a.open_time ?? "";  bv = b.open_time ?? ""; }
            else if (sortKey === "close_time") { av = a.close_time ?? ""; bv = b.close_time ?? ""; }
            else if (sortKey === "symbol")     { av = a.symbol;   bv = b.symbol; }
            else if (sortKey === "direction")  { av = a.direction; bv = b.direction; }
            else if (sortKey === "volume")     { av = a.volume;   bv = b.volume; }
            else if (sortKey === "entry")      { av = a.entry;    bv = b.entry; }
            else if (sortKey === "pnl")        { av = getPnl(a) ?? -Infinity; bv = getPnl(b) ?? -Infinity; }
            else if (sortKey === "trading_type") { av = a.trading_type ?? ""; bv = b.trading_type ?? ""; }
            if (av === null) av = "";
            if (bv === null) bv = "";
            const cmp = av < bv ? -1 : av > bv ? 1 : 0;
            return sortDir === "asc" ? cmp : -cmp;
          });

          const handleSort = (key: SortKey) => {
            if (sortKey === key) setSortDir(d => d === "asc" ? "desc" : "asc");
            else { setSortKey(key); setSortDir("desc"); }
          };

          const SortIcon = ({ col }: { col: SortKey }) => (
            <span className="ml-1 inline-block text-gray-600">
              {sortKey === col ? (sortDir === "asc" ? "▲" : "▼") : "⇅"}
            </span>
          );

          return (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-gray-800 select-none">
                  {(
                    [
                      ["ticket",       "Ticket"],
                      ["open_time",    "Entry Time"],
                      ["close_time",   "Close Time"],
                      ["symbol",       "Symbol"],
                      ["direction",    "Dir"],
                      ["volume",       "Lots"],
                      ["entry",        "Entry"],
                      [null,           "SL"],
                      [null,           "TP"],
                      ["pnl",          "P&L"],
                      ["trading_type", "Type"],
                      [null,           "Account"],
                      [null,           "Status"],
                    ] as [SortKey | null, string][]
                  ).map(([col, label]) =>
                    col ? (
                      <th
                        key={label}
                        className="text-left py-2 pr-4 cursor-pointer hover:text-gray-300 transition-colors whitespace-nowrap"
                        onClick={() => handleSort(col)}
                      >
                        {label}<SortIcon col={col} />
                      </th>
                    ) : (
                      <th key={label} className="text-left py-2 pr-4">{label}</th>
                    )
                  )}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800">
                {rows.map((e, i) => {
                  const isClosed = e.event === "close";
                  const live = !isClosed ? liveProfit[e.ticket] : undefined;
                  const pnl  = getPnl(e);
                  const isLive = e.profit == null && live != null;
                  return (
                    <tr key={i} className="text-gray-300 hover:bg-gray-800/50 transition-colors">
                      <td className="py-2 pr-4 font-mono text-gray-500">#{e.ticket}</td>
                      <td className="py-2 pr-4 font-mono text-gray-400 whitespace-nowrap">{fmtTime(e.open_time)}</td>
                      <td className="py-2 pr-4 font-mono text-gray-400 whitespace-nowrap">{fmtTime(e.close_time)}</td>
                      <td className="py-2 pr-4 font-semibold">{e.symbol}</td>
                      <td className={`py-2 pr-4 font-semibold ${e.direction === "buy" ? "text-green-400" : "text-red-400"}`}>
                        {e.direction.toUpperCase()}
                      </td>
                      <td className="py-2 pr-4">{e.volume}</td>
                      <td className="py-2 pr-4 font-mono">{e.entry}</td>
                      <td className="py-2 pr-4 font-mono text-red-400">{e.sl || "—"}</td>
                      <td className="py-2 pr-4 font-mono text-green-400">{e.tp || "—"}</td>
                      <td className={`py-2 pr-4 font-mono ${pnl == null ? "text-gray-500" : pnl >= 0 ? "text-green-400" : "text-red-400"}`}>
                        {pnl == null ? "—" : `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}${isLive ? " ~" : ""}`}
                      </td>
                      <td className="py-2 pr-4 text-gray-500">{e.trading_type}</td>
                      <td className="py-2 pr-4">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${
                          e.account_mode === "live" ? "bg-red-900/40 text-red-300" : "bg-emerald-900/40 text-emerald-300"
                        }`}>{e.account_mode}</span>
                      </td>
                      <td className="py-2">
                        <span className={`px-1.5 py-0.5 rounded text-xs font-semibold ${
                          isClosed ? "bg-gray-700 text-gray-300" : "bg-blue-900/40 text-blue-300"
                        }`}>{isClosed ? "Closed" : "Open"}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          );
        })()}
      </div>
    </div>
  );
}
