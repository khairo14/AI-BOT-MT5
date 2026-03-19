"use client";
import { useEffect, useRef, useState } from "react";
import { useBotStore } from "@/lib/store";
import { fetchLogTail } from "@/lib/api";

function lineColor(line: string): string {
  const u = line.toUpperCase();
  if (u.includes("ERROR") || u.includes("CRITICAL")) return "text-red-400";
  if (u.includes("WARNING") || u.includes("WARN")) return "text-yellow-400";
  if (u.includes("SUCCESS") || u.includes("CONNECTED")) return "text-emerald-400";
  return "text-gray-300";
}

export default function LogConsole() {
  const { logLines, setLogLines, appendLogLines } = useBotStore();
  const [open, setOpen] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastCountRef = useRef(0);

  // Initial load + polling every 3 s when open
  useEffect(() => {
    fetchLogTail(200)
      .then((r) => setLogLines(r.lines))
      .catch(() => {});

    if (!open) return;

    pollRef.current = setInterval(() => {
      fetchLogTail(200)
        .then((r) => {
          if (r.lines.length > lastCountRef.current) {
            const newLines = r.lines.slice(lastCountRef.current);
            appendLogLines(newLines);
            lastCountRef.current = r.lines.length;
          } else {
            lastCountRef.current = r.lines.length;
          }
        })
        .catch(() => {});
    }, 3000);

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (autoScroll && open) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [logLines, autoScroll, open]);

  return (
    <div className="border-t border-gray-800 bg-gray-950">
      {/* Header bar */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-4 py-2 text-xs text-gray-400 hover:text-white hover:bg-gray-900 transition-colors"
      >
        <span className="font-mono font-bold tracking-widest text-gray-600">&gt;_</span>
        <span className="font-semibold">API Logs</span>
        <span className="text-gray-600 ml-1">({logLines.length} lines)</span>
        <span className="ml-auto">{open ? "▼" : "▲"}</span>
      </button>

      {open && (
        <div className="relative">
          {/* Toolbar */}
          <div className="flex items-center gap-3 px-4 py-1.5 border-b border-gray-800 bg-gray-900">
            <button
              onClick={() => {
                fetchLogTail(200)
                  .then((r) => { setLogLines(r.lines); lastCountRef.current = r.lines.length; })
                  .catch(() => {});
              }}
              className="text-xs text-gray-500 hover:text-white transition-colors"
            >
              ↻ Refresh
            </button>
            <label className="flex items-center gap-1.5 text-xs text-gray-500 cursor-pointer">
              <input
                type="checkbox"
                checked={autoScroll}
                onChange={(e) => setAutoScroll(e.target.checked)}
                className="accent-blue-500"
              />
              Auto-scroll
            </label>
            <button
              onClick={() => setLogLines([])}
              className="text-xs text-gray-600 hover:text-red-400 transition-colors ml-auto"
            >
              Clear
            </button>
          </div>

          {/* Log output */}
          <div className="h-48 overflow-y-auto font-mono text-xs bg-gray-950 px-4 py-2 space-y-0.5">
            {logLines.length === 0 ? (
              <p className="text-gray-700 italic">No log output yet. Start the bot to see live logs.</p>
            ) : (
              logLines.map((line, i) => (
                <p key={i} className={`leading-5 whitespace-pre-wrap break-all ${lineColor(line)}`}>
                  {line}
                </p>
              ))
            )}
            <div ref={bottomRef} />
          </div>
        </div>
      )}
    </div>
  );
}
