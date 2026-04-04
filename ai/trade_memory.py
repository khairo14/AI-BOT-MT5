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
    lstm_predicted_direction: Optional[str] = None  # "BUY" or "SELL" — LSTM prediction at signal time
    extra:           dict = field(default_factory=dict)

class TradeMemory:
    """
    Thread-safe trade outcome store. Appends to JSONL on disk and
    keeps a rolling in-memory buffer for fast RL training reads.
    """

    MAX_BUFFER = 10_000   # keep last N outcomes in memory

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

    def stats(self, trading_type: Optional[str] = None, live_only: bool = False, mode: Optional[str] = None, exclude_manual: bool = False) -> dict:
        """Aggregate stats used by the RL agent and dashboard."""
        outcomes = self.recent(n=self.MAX_BUFFER, trading_type=trading_type, live_only=live_only, mode=mode)
        if exclude_manual:
            outcomes = [o for o in outcomes if o.get("outcome") in ("tp_hit", "sl_hit")]
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
    
    def lstm_accuracy(
        self,
        trading_type: Optional[str] = None,
        min_samples: int = 20,
        live_only: bool = True,
    ) -> dict:
        """
        Compute live LSTM prediction accuracy by comparing lstm_predicted_direction
        against actual trade outcome direction.

        A prediction is 'correct' when:
          - LSTM predicted BUY and trade was profitable (price went up)
          - LSTM predicted SELL and trade was profitable (price went down)

        Returns dict with accuracy per symbol and overall, or empty if insufficient data.
        Requires lstm_predicted_direction to be populated in TradeOutcome.extra or field.
        """
        outcomes = self.recent(
            n=self.MAX_BUFFER,
            trading_type=trading_type,
            live_only=live_only,
        )
        # Only consider trades where LSTM prediction was recorded
        tracked = [
            o for o in outcomes
            if o.get("lstm_predicted_direction") or
            o.get("extra", {}).get("lstm_predicted_direction")
        ]
        if len(tracked) < min_samples:
            return {"tracked": len(tracked), "min_samples": min_samples, "sufficient_data": False}

        def _get_pred(o: dict) -> Optional[str]:
            return (
                o.get("lstm_predicted_direction") or
                o.get("extra", {}).get("lstm_predicted_direction")
            )

        correct = 0
        total   = 0
        by_symbol: dict[str, dict] = {}

        for o in tracked:
            pred   = _get_pred(o)
            actual = o.get("direction", "").upper()
            profit = o.get("profit", 0)
            if not pred or not actual:
                continue
            # Correct if prediction matches actual profitable direction
            is_correct = (
                (pred == "BUY"  and profit > 0 and actual == "BUY") or
                (pred == "SELL" and profit > 0 and actual == "SELL") or
                (pred == "BUY"  and profit < 0 and actual == "SELL") or
                (pred == "SELL" and profit < 0 and actual == "BUY")
            )
            # Simpler: prediction correct if market went the predicted direction
            is_correct = (pred == actual and profit > 0) or (pred != actual and profit < 0)
            correct += int(is_correct)
            total   += 1

            sym = o.get("symbol", "unknown")
            if sym not in by_symbol:
                by_symbol[sym] = {"correct": 0, "total": 0}
            by_symbol[sym]["correct"] += int(is_correct)
            by_symbol[sym]["total"]   += 1

        overall_acc = round(correct / total, 4) if total else 0.0
        sym_accuracy = {
            sym: {
                "accuracy": round(v["correct"] / v["total"], 4),
                "samples":  v["total"],
            }
            for sym, v in by_symbol.items()
            if v["total"] >= 5  # min 5 samples per symbol
        }

        # Flag symbols where live LSTM accuracy is worse than random (< 45%)
        degraded = [
            sym for sym, v in sym_accuracy.items()
            if v["accuracy"] < 0.45
        ]

        return {
            "overall_accuracy": overall_acc,
            "correct":          correct,
            "total":            total,
            "sufficient_data":  True,
            "by_symbol":        sym_accuracy,
            "degraded_symbols": degraded,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    # ── internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load existing JSONL file into memory buffer at startup."""
        if not MEMORY_FILE.exists():
            return
        parsed = []
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as exc:
            from loguru import logger
            logger.warning(f"TradeMemory: could not open {MEMORY_FILE}: {exc}")
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                parsed.append(json.loads(line))
            except Exception as exc:
                from loguru import logger
                logger.warning(f"TradeMemory: skipping corrupt line in {MEMORY_FILE}: {exc}")
        self._buffer = parsed[-self.MAX_BUFFER:]


# Application-level singleton
memory = TradeMemory()
