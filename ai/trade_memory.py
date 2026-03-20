"""
Trade Memory — persistent store for closed trade outcomes.

Each entry records what the RL agent needs to learn from:
  - which strategy + symbol + direction fired
  - signal confidence at entry time
  - actual P&L (pips, money, %)
  - whether SL or TP was hit (outcome type)
  - hold duration in minutes

Stored as a JSONL file: ai/data/trade_memory.jsonl
One JSON object per line for easy appending and streaming reads.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

MEMORY_FILE = DATA_DIR / "trade_memory.jsonl"


@dataclass
class TradeOutcome:
    ticket:          int
    symbol:          str
    strategy:        str
    trading_type:    str          # scalping | day_trading | swing
    direction:       str          # BUY | SELL
    confidence:      float        # 0–1 score at signal time
    entry_price:     float
    close_price:     float
    sl_price:        float
    tp_price:        float
    volume:          float
    profit:          float        # money P&L (account currency)
    profit_pips:     float        # pips P&L
    profit_pct:      float        # % of account balance at open
    outcome:         str          # "tp_hit" | "sl_hit" | "manual_close" | "unknown"
    open_time:       str          # ISO datetime
    close_time:      str          # ISO datetime
    duration_mins:   float
    mode:            str  = "live"   # "live" or "paper" — used to separate RL/stats per mode
    extra:           dict = field(default_factory=dict)


class TradeMemory:
    """
    Thread-safe trade outcome store. Appends to JSONL on disk and
    keeps a rolling in-memory buffer for fast RL training reads.
    """

    MAX_BUFFER = 5_000   # keep last N outcomes in memory

    def __init__(self):
        self._lock   = threading.Lock()
        self._buffer: list[dict] = []
        self._load()

    # ── public API ────────────────────────────────────────────────────────────

    def record(self, outcome: TradeOutcome) -> None:
        """Append a closed trade outcome to memory."""
        entry = asdict(outcome)
        entry["recorded_at"] = datetime.now(timezone.utc).isoformat() + "Z"
        with self._lock:
            self._buffer.append(entry)
            if len(self._buffer) > self.MAX_BUFFER:
                self._buffer = self._buffer[-self.MAX_BUFFER:]
            with open(MEMORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def recent(self, n: int = 200, trading_type: Optional[str] = None, live_only: bool = False, mode: Optional[str] = None) -> list[dict]:
        """Return the last N outcomes, optionally filtered by trading_type and/or mode.
        live_only=True excludes backtest entries so RL/optimizer aren't skewed by re-runs."""
        with self._lock:
            data = list(self._buffer)
        if trading_type:
            data = [d for d in data if d.get("trading_type") == trading_type]
        if mode:
            data = [d for d in data if d.get("mode", "live") == mode]
        if live_only:
            data = [d for d in data if d.get("extra", {}).get("source") != "backtest"]
        return data[-n:]

    def stats(self, trading_type: Optional[str] = None, live_only: bool = False, mode: Optional[str] = None) -> dict:
        """Aggregate stats used by the RL agent and dashboard."""
        outcomes = self.recent(n=self.MAX_BUFFER, trading_type=trading_type, live_only=live_only, mode=mode)
        if not outcomes:
            return {"total": 0}
        total   = len(outcomes)
        wins    = sum(1 for o in outcomes if o["profit"] > 0)
        losses  = sum(1 for o in outcomes if o["profit"] <= 0)
        avg_pnl = sum(o["profit"] for o in outcomes) / total
        avg_conf= sum(o["confidence"] for o in outcomes) / total
        tp_hits = sum(1 for o in outcomes if o["outcome"] == "tp_hit")
        sl_hits = sum(1 for o in outcomes if o["outcome"] == "sl_hit")
        return {
            "total":      total,
            "wins":       wins,
            "losses":     losses,
            "win_rate":   round(wins / total, 4) if total else 0,
            "avg_pnl":    round(avg_pnl, 4),
            "avg_conf":   round(avg_conf, 4),
            "tp_hits":    tp_hits,
            "sl_hits":    sl_hits,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ── internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load existing JSONL file into memory buffer at startup."""
        if not MEMORY_FILE.exists():
            return
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            parsed = []
            for line in lines:
                line = line.strip()
                if line:
                    parsed.append(json.loads(line))
            self._buffer = parsed[-self.MAX_BUFFER:]
        except Exception as exc:
            from loguru import logger
            logger.warning(f"TradeMemory: could not load {MEMORY_FILE}: {exc}")


# Application-level singleton
memory = TradeMemory()
