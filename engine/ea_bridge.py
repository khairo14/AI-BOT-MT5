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
            # MT5 TimeCurrent() returns broker server time (≈ UTC).
            # Compare against local UTC time — difference should be minimal.
            local_ts = int(time.time())
            age = abs(local_ts - mt5_ts)
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

        cmd = {
            "id":         sig_id,
            "action":     "open",
            "symbol":     signal.get("symbol", ""),
            "direction":  signal.get("direction", "").upper(),
            "volume":     float(signal.get("lot_size") or 0.01),
            "sl":         float(signal.get("sl") or 0),
            "tp":         float(signal.get("tp") or 0),
            "comment":    f"scalp|{signal.get('strategy', '?')[:20]}",
            "magic":      20260318,
            # Unix timestamp — EA uses this for stale-guard
            "created_ts": int(time.time()),
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
        """Resolve and cache the MT5 common data path."""
        if self._base_dir is not None and self._base_dir.exists():
            return self._base_dir

        try:
            import MetaTrader5 as mt5
            info = mt5.terminal_info()
            if info is None:
                return None
            # terminal_info().commondata_path → e.g.
            # C:\Users\<user>\AppData\Roaming\MetaQuotes\Terminal\Common
            common = Path(info.commondata_path) / "Files" / "evotrade"
            common.mkdir(parents=True, exist_ok=True)
            self._base_dir = common
            return common
        except Exception as exc:
            logger.debug(f"EABridge._get_dir error: {exc}")
            return None


# Application-level singleton
ea_bridge = EABridge()
