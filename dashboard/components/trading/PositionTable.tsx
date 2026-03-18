"use client";
import { closePosition } from "@/lib/api";
import { useBotStore } from "@/lib/store";
import type { Position } from "@/types";

interface Props {
  positions: Position[];
  onRefresh: () => void;
}

export default function PositionTable({ positions, onRefresh }: Props) {
  const { pushNotification } = useBotStore();

  const handleClose = async (ticket: number) => {
    try {
      await closePosition(ticket);
      pushNotification({ type: "success", title: "Position closed", message: `Ticket #${ticket}` });
      onRefresh();
    } catch {
      pushNotification({ type: "error", title: "Close failed", message: `Ticket #${ticket}` });
    }
  };

  if (!positions.length) {
    return (
      <p className="text-gray-600 text-sm text-center py-6">No open positions</p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm text-left">
        <thead>
          <tr className="text-xs text-gray-500 uppercase border-b border-gray-800">
            <th className="pb-2 pr-4">Symbol</th>
            <th className="pb-2 pr-4">Type</th>
            <th className="pb-2 pr-4">Lots</th>
            <th className="pb-2 pr-4">Open</th>
            <th className="pb-2 pr-4">SL</th>
            <th className="pb-2 pr-4">TP</th>
            <th className="pb-2 pr-4">P&L</th>
            <th className="pb-2"></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-800/50">
          {positions.map((p) => (
            <tr key={p.ticket} className="text-gray-300">
              <td className="py-2 pr-4 font-medium text-white">{p.symbol}</td>
              <td className={`py-2 pr-4 font-semibold ${p.type === "buy" ? "text-emerald-400" : "text-red-400"}`}>
                {p.type.toUpperCase()}
              </td>
              <td className="py-2 pr-4">{p.volume}</td>
              <td className="py-2 pr-4 font-mono">{p.open_price}</td>
              <td className="py-2 pr-4 font-mono text-red-400">{p.sl || "—"}</td>
              <td className="py-2 pr-4 font-mono text-emerald-400">{p.tp || "—"}</td>
              <td className={`py-2 pr-4 font-mono font-semibold ${p.profit >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                {p.profit >= 0 ? "+" : ""}${p.profit.toFixed(2)}
              </td>
              <td className="py-2">
                <button
                  onClick={() => handleClose(p.ticket)}
                  className="text-xs px-2 py-1 rounded bg-gray-800 hover:bg-red-900 text-gray-400 hover:text-red-300 transition-colors"
                >
                  Close
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
