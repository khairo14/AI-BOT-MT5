"""
EA Bridge — Python side of the MQL5 AIBotScalper communication protocol.

How it works:
  - Python writes a command file: Common/Files/evotrade/ea_cmd_<id>.json
  - The EA picks it up on the next tick (typically <50 ms), executes the
    order natively, then writes: Common/Files/evotrade/ea_res_<id>.json
  - Python polls for the result file (up to `timeout` seconds) and reads it

Path resolution:
  Uses mt5.terminal_info().commondata_path at runtime so it works regardless
  of where MT5 is installed. No hardcoded paths.

Heartbeat:
  The EA writes Common/Files/evotrade/ea_heartbeat.txt (a Unix timestamp)
  every second. is_active() confirms the EA is alive and recently heartbeated.
  If the EA is not running, submit_and_wait() falls back to Python execution
  transparently.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

_HEARTBEAT_FILE = "evotrade/ea_heartbeat.txt"
_CMD_PREFIX     = "evotrade/ea_cmd_"
_RES_PREFIX     = "evotrade/ea_res_"
_HEARTBEAT_MAX_AGE = 10   # seconds — EA considered dead if older than this
_POLL_INTERVAL     = 0.05  # 50 ms poll loop


class EABridge:
    """
    Coordinates signal dispatch to the MQL5 AIBotScalper EA.

    Thread-safe: each call uses a per-signal file so no locking needed.
    Falls back gracefully to Python execution if EA is not active.
    """

    def __init__(self):
        self._base_dir: Optional[Path] = None

    # ── public API ────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        """Return True if the EA is running and heartbeating within the last 10 s."""
        d = self._get_dir()
        if d is None:
            return False
        hb = d / "ea_heartbeat.txt"
        if not hb.exists():
            return False
        try:
            mt5_ts = int(hb.read_text().strip())
            # MT5 TimeCurrent() returns broker server time (e.g. EET = UTC+2/+3).
            # Comparing to time.time() (UTC epoch) creates a ~7200 s false offset.
            # Instead, get broker time from a live tick — same clock source as TimeCurrent().
            import MetaTrader5 as _mt5
            broker_now: int = int(time.time())   # fallback
            for sym in ("EURUSD", "GBPUSD", "USDJPY"):
                tick = _mt5.symbol_info_tick(sym)
                if tick is not None:
                    broker_now = int(tick.time)
                    break
            age = abs(broker_now - mt5_ts)
            return age < _HEARTBEAT_MAX_AGE
        except Exception:
            return False

    def submit_and_wait(
        self,
        signal: dict,
        timeout: float = 5.0,
    ) -> bool:
        """
        Write a command file for the EA, wait for the result, and annotate
        the signal dict with ticket + fill_price (mirrors what order_manager does).

        Returns True on success, False on failure/timeout.
        Callers should fall back to Python execution on False.
        """
        d = self._get_dir()
        if d is None:
            logger.warning("EABridge: cannot determine MT5 common data path — falling back to Python")
            return False

        sig_id = signal.get("id", "")
        if not sig_id:
            logger.error("EABridge: signal missing 'id' field")
            return False

        from engine.order_manager import BOT_MAGIC as _BOT_MAGIC

        # ATR-based trailing stop / breakeven distances.
        # The strategy stores `atr` in its indicators dict; ea_bridge converts it
        # to pip distances using the configured multipliers so the EA trails each
        # trade proportionally to that symbol's actual volatility instead of a
        # fixed global default that is too tight for exotics and commodities.
        # Fallback to 0.0 tells the EA to use its own InpTrailingStopPips default.
        _indicators = signal.get("indicators") or {}
        _atr = float(_indicators.get("atr") or 0.0)
        _trail_mult = 1.0   # trail at 1.0 × ATR (expressed in price units)
        _be_mult    = 0.6   # breakeven at 0.6 × ATR above entry
        # Convert ATR (price units) to pips.  Use entry price to determine pip size:
        # 5-digit forex (e.g. 1.08xxx): pip = 0.0001, so pips = atr / 0.0001
        # 3-digit forex (e.g. 110.xxx): pip = 0.01
        # Commodities / indices: just use 1.0 (raw points, matches _GetEffectivePipSize)
        _entry = float(signal.get("entry_price") or signal.get("sl") or 1.0)
        if _entry > 10.0:          # JPY pairs, metals, indices — pip = 0.01 or 1.0
            _pip = 0.01 if _entry < 1000.0 else 1.0
        else:                      # standard 5-digit forex pairs
            _pip = 0.0001
        _trail_pips = round(_atr * _trail_mult / _pip, 1) if _atr > 0 else 0.0
        _be_pips    = round(_atr * _be_mult    / _pip, 1) if _atr > 0 else 0.0

        cmd = {
            "id":         sig_id,
            "action":     "open",
            "symbol":     signal.get("symbol", ""),
            "direction":  signal.get("direction", "").upper(),
            "volume":     float(signal.get("lot_size") or 0.01),
            "sl":         float(signal.get("sl") or 0),
            "tp":         float(signal.get("tp") or 0),
            "comment":    f"scalp|{signal.get('strategy', '?')[:20]}",
            "magic":      _BOT_MAGIC,
            # Unix timestamp — EA uses this for stale-guard
            "created_ts": int(time.time()),
            # Per-trade ATR-based trail/breakeven (0.0 = use EA defaults)
            "trail_pips": _trail_pips,
            "be_pips":    _be_pips,
        }

        cmd_file = d / f"ea_cmd_{sig_id}.json"
        res_file = d / f"ea_res_{sig_id}.json"

        try:
            cmd_file.write_text(json.dumps(cmd))
        except OSError as exc:
            logger.error(f"EABridge: failed to write command file: {exc}")
            return False

        logger.debug(f"EABridge: command written [{sig_id[:8]}] {cmd['symbol']} {cmd['direction']}")

        # Poll for result
        deadline = time.time() + timeout
        while time.time() < deadline:
            if res_file.exists():
                return self._consume_result(res_file, signal)
            time.sleep(_POLL_INTERVAL)

        # Timeout — clean up orphaned command file
        try:
            cmd_file.unlink(missing_ok=True)
        except OSError:
            pass

        logger.warning(
            f"EABridge: timeout ({timeout}s) waiting for EA result "
            f"[{sig_id[:8]}] {cmd['symbol']} — will fall back to Python"
        )
        return False

    # ── internals ─────────────────────────────────────────────────────────────

    def _consume_result(self, res_file: Path, signal: dict) -> bool:
        try:
            data = json.loads(res_file.read_text())
            res_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.error(f"EABridge: cannot read result file: {exc}")
            return False

        status = data.get("status", "error")
        if status == "ok":
            signal["ticket"]     = data.get("ticket")
            signal["fill_price"] = data.get("fill_price")
            logger.info(
                f"EABridge: order filled via EA | "
                f"ticket={signal['ticket']} fill={signal['fill_price']} "
                f"[{signal.get('id','')[:8]}] {signal.get('symbol')} {signal.get('direction')}"
            )
            return True
        else:
            error = data.get("error", "unknown EA error")
            signal["error"] = error
            logger.error(
                f"EABridge: EA rejected order [{signal.get('id','')[:8]}] "
                f"{signal.get('symbol')}: {error}"
            )
            return False

    def _get_dir(self) -> Optional[Path]:
        """Resolve and cache the MT5 common data path. Cleans up orphan cmd files on first call."""
        if self._base_dir is not None and self._base_dir.exists():
            return self._base_dir

        try:
            import MetaTrader5 as mt5
            info = mt5.terminal_info()  # type: ignore[attr-defined]
            if info is None:
                return None
            # terminal_info().commondata_path → e.g.
            # C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\Common
            common = Path(info.commondata_path) / "Files" / "evotrade"
            common.mkdir(parents=True, exist_ok=True)
            self._base_dir = common
            # G-8: clean up orphaned ea_cmd_*.json files left by a previous crash
            _now = time.time()
            for orphan in common.glob("ea_cmd_*.json"):
                try:
                    if _now - orphan.stat().st_mtime > 30:
                        orphan.unlink(missing_ok=True)
                        logger.debug(f"EABridge: removed orphan {orphan.name}")
                except OSError:
                    pass
            return common
        except Exception as exc:
            logger.debug(f"EABridge._get_dir error: {exc}")
            return None


# Application-level singleton
ea_bridge = EABridge()
