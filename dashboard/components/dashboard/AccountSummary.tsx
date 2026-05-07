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
    <div className="rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-3">
      <p className="text-xs uppercase tracking-wide text-slate-400">{label}</p>
      <p className={`mt-1 text-sm font-semibold ${color || "text-slate-100"}`}>
        {value}
      </p>
    </div>
  );
}

function money(value?: number | null, currency = "USD") {
  const safeValue = Number(value ?? 0);

  return `${currency} ${safeValue.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

function normalizeAccountMode(account: AccountInfo | null) {
  const rawMode = String(
    account?.account_type || account?.mode || ""
  ).toLowerCase();

  if (rawMode === "demo" || rawMode === "paper") return "demo";
  if (rawMode === "live") return "live";

  return "unknown";
}

export default function AccountSummary({ account }: Props) {
  const mode = normalizeAccountMode(account);
  const isDemo = mode === "demo";
  const isLive = mode === "live";

  const badgeText = isDemo ? "DEMO" : isLive ? "LIVE" : "UNKNOWN";

  const badgeClass = isDemo
    ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
    : isLive
      ? "border-red-500/40 bg-red-500/10 text-red-300"
      : "border-slate-500/40 bg-slate-500/10 text-slate-300";

  if (!account) {
    return (
      <section className="rounded-2xl border border-slate-800 bg-slate-950/80 p-5 shadow-lg">
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-xs uppercase tracking-wide text-slate-500">
              Account
            </p>
            <h2 className="mt-1 text-lg font-semibold text-slate-100">
              No account connected
            </h2>
          </div>

          <span className="rounded-full border border-slate-700 bg-slate-900 px-3 py-1 text-xs font-bold text-slate-400">
            OFFLINE
          </span>
        </div>
      </section>
    );
  }

  const currency = account.currency || "USD";

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-950/80 p-5 shadow-lg">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Account
          </p>

          <div className="mt-1 flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-100">
              {account.login || "Unknown Login"}
            </h2>

            <span
              className={`rounded-full border px-3 py-1 text-xs font-bold ${badgeClass}`}
            >
              {badgeText}
            </span>
          </div>

          <p className="mt-1 text-sm text-slate-400">
            {account.server || "Unknown Server"}
          </p>
        </div>

        <div className="text-right">
          <p className="text-xs uppercase tracking-wide text-slate-500">
            Currency
          </p>
          <p className="mt-1 text-sm font-semibold text-slate-100">
            {currency}
          </p>
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Balance" value={money(account.balance, currency)} />
        <Stat label="Equity" value={money(account.equity, currency)} />
        <Stat label="Free Margin" value={money(account.free_margin, currency)} />
        <Stat
          label="Profit"
          value={money(account.profit, currency)}
          color={Number(account.profit || 0) >= 0 ? "text-emerald-300" : "text-red-300"}
        />
      </div>
    </section>
  );
}