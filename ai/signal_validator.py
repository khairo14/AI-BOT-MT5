from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from datetime import datetime, timezone, timedelta


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

SIGNAL_JOURNAL_FILE = DATA_DIR / "signal_journal.jsonl"
VALIDATED_SIGNAL_FILE = DATA_DIR / "validated_signal_memory.jsonl"

def normalize_execution_mode(value: Any = None) -> str:
    value = str(value or "live").lower().strip()
    return "demo" if value == "paper" else value
@dataclass
class SignalValidationResult:
    signal_id: str
    validated: bool
    validated_outcome: str = "unknown"  # would_tp | would_sl | expired | unknown
    future_profit_pips: Optional[float] = None
    future_profit_pct: Optional[float] = None
    max_favorable_pips: Optional[float] = None
    max_adverse_pips: Optional[float] = None
    validation_errors: list[str] = field(default_factory=list)
    validated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SignalValidator:
    """
    Validates shadow/rejected/skipped signals after enough future candles exist.

    This does NOT place trades.
    This does NOT directly train RL.

    It only converts signal_journal entries into validated shadow outcomes.
    """

    def __init__(
        self,
        signal_journal_file: Path = SIGNAL_JOURNAL_FILE,
        validated_signal_file: Path = VALIDATED_SIGNAL_FILE,
    ):
        self.signal_journal_file = signal_journal_file
        self.validated_signal_file = validated_signal_file

    def load_pending_signals(self, limit: int = 500) -> list[dict]:
        signals = self._read_jsonl(self.signal_journal_file)

        pending = [
            s for s in signals
            if not s.get("validated")
            and s.get("status") in {"shadow", "rejected", "blocked", "missed", "expired"}
        ]

        return pending[-limit:]

    def validate_signal(self, signal: dict, future_bars: list[dict]) -> dict:
        """
        future_bars should be candles after the signal time.

        Expected candle shape:
        {
            "time": "...",
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "close": 0.0
        }
        """
        errors: list[str] = []

        signal_id = str(
            signal.get("signal_id")
            or signal.get("id")
            or f"{signal.get('symbol_normalized', 'UNKNOWN')}_{signal.get('recorded_at', '')}"
        )

        signal["execution_mode"] = normalize_execution_mode(
            signal.get("execution_mode") or signal.get("account_mode")
        )

        direction = str(signal.get("direction", "")).upper().strip()
        entry = self._safe_float(signal.get("entry") or signal.get("entry_price"))
        sl = self._safe_float(signal.get("sl") or signal.get("sl_price"))
        tp = self._safe_float(signal.get("tp") or signal.get("tp_price"))

        if direction not in {"BUY", "SELL"}:
            errors.append("invalid_direction")
        if entry is None or entry <= 0:
            errors.append("invalid_entry")
        if sl is None or sl <= 0:
            errors.append("invalid_sl")
        if tp is None or tp <= 0:
            errors.append("invalid_tp")
        if not future_bars:
            errors.append("missing_future_bars")

        if errors:
            return SignalValidationResult(
                signal_id=signal_id,
                validated=False,
                validation_errors=errors,
            ).__dict__

        assert entry is not None
        assert sl is not None
        assert tp is not None

        outcome = "expired"
        max_favorable = 0.0
        max_adverse = 0.0
        final_close = entry

        for bar in future_bars:
            high = self._safe_float(bar.get("high"))
            low = self._safe_float(bar.get("low"))
            close = self._safe_float(bar.get("close"))

            if high is None or low is None or close is None:
                continue

            final_close = close

            if direction == "BUY":
                if high is not None and entry is not None:
                    max_favorable = max(max_favorable, high - entry)

                if low is not None and entry is not None:
                    max_adverse = min(max_adverse, low - entry)

                # Conservative rule: if both hit in same candle, count SL first.
                if low <= sl:
                    outcome = "would_sl"
                    final_close = sl
                    break
                if high >= tp:
                    outcome = "would_tp"
                    final_close = tp
                    break

            else:
                if low is not None and entry is not None:
                    max_favorable = max(max_favorable, entry - low)

                if high is not None and entry is not None:
                    max_adverse = min(max_adverse, entry - high)

                # Conservative rule: if both hit in same candle, count SL first.
                if high >= sl:
                    outcome = "would_sl"
                    final_close = sl
                    break
                if low <= tp:
                    outcome = "would_tp"
                    final_close = tp
                    break

        if final_close is None or entry is None:
            future_profit_pips = None
        else:
            future_profit_pips = self._price_to_pips(
                signal,
                final_close - entry,
                direction,
            )

        result = SignalValidationResult(
            signal_id=signal_id,
            validated=True,
            validated_outcome=outcome,
            future_profit_pips=future_profit_pips,
            future_profit_pct=None,
            max_favorable_pips=self._price_to_pips(signal, max_favorable, direction),
            max_adverse_pips=self._price_to_pips(signal, max_adverse, direction),
            validation_errors=[],
        )

        return {
            **signal,
            **result.__dict__,
        }

    def record_validated(self, result: dict) -> None:
        payload = {
            **result,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }

        with open(self.validated_signal_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")

    def _read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []

        rows: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if isinstance(payload, dict):
                        rows.append(payload)
                except Exception:
                    continue

        return rows

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None

    def _price_to_pips(self, signal: dict, price_diff: float, direction: str) -> float:
        """
        Basic pip conversion fallback.

        Later we should replace this with symbol_info-based pip size
        from MT5 for metals, crypto, indices, and futures.
        """
        symbol = str(signal.get("symbol_normalized") or signal.get("symbol") or "").upper()

        pip_size = 0.01 if "JPY" in symbol else 0.0001

        if direction == "SELL":
            price_diff = -price_diff

        return round(price_diff / pip_size, 2)

    def fetch_future_bars_for_signal(
        self,
        signal: dict,
        lookahead_bars: int = 100,
    ) -> list[dict]:
        try:
            from engine.mt5_client import MT5Client
        except Exception:
            from engine.mt5_client import MT5Client

        symbol = (
            signal.get("symbol_raw")
            or signal.get("symbol")
            or signal.get("symbol_normalized")
        )

        timeframe = (
            signal.get("timeframe")
            or signal.get("tf")
            or self._timeframe_from_trading_type(signal.get("trading_type"))
        )

        recorded_at = signal.get("recorded_at") or signal.get("signal_time")
        if not symbol or not recorded_at:
            return []

        start = self._parse_datetime(recorded_at)
        if start is None:
            return []

        end = start + self._lookahead_delta(timeframe, lookahead_bars)

        client = MT5Client()
        if not client.connect():
            return []

        try:
            df = client.get_ohlcv_range(
                symbol=str(symbol),
                timeframe=str(timeframe),
                date_from=start,
                date_to=end,
            )
        finally:
            client.disconnect()

        if df is None or df.empty:
            return []

        return df.to_dict("records")
    
    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

        try:
            text = str(value).replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _timeframe_from_trading_type(self, trading_type: Any) -> str:
        trading_type = str(trading_type or "").lower().strip()

        if trading_type == "scalping":
            return "M5"
        if trading_type == "swing":
            return "H4"

        return "H1"

    def _lookahead_delta(self, timeframe: str, bars: int) -> timedelta:
        tf = str(timeframe or "H1").upper()

        minutes_by_tf = {
            "M1": 1,
            "M2": 2,
            "M5": 5,
            "M15": 15,
            "M30": 30,
            "H1": 60,
            "H4": 240,
            "D1": 1440,
        }

        minutes = minutes_by_tf.get(tf, 60)
        return timedelta(minutes=minutes * bars)
    
    def validate_pending_signals(self, max_signals: int = 100) -> dict:
        pending = self.load_pending_signals(limit=max_signals)
        already_validated = self._validated_signal_ids()

        checked = 0
        validated = 0
        skipped = 0

        for signal in pending:
            signal_id = str(signal.get("signal_id", ""))

            if signal_id and signal_id in already_validated:
                skipped += 1
                continue

            checked += 1
            future_bars = self.fetch_future_bars_for_signal(signal)

            if not future_bars:
                skipped += 1
                continue

            result = self.validate_signal(signal, future_bars)

            if result.get("validated"):
                self.record_validated(result)
                validated += 1
            else:
                skipped += 1

        return {
            "checked": checked,
            "validated": validated,
            "skipped": skipped,
        }
    
    def _validated_signal_ids(self) -> set[str]:
        rows = self._read_jsonl(self.validated_signal_file)
        return {
            str(r.get("signal_id"))
            for r in rows
            if r.get("signal_id")
        }
    
signal_validator = SignalValidator()