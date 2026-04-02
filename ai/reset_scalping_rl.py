"""
Reset Scalping RL Agent
========================
Resets ONLY the scalping Q-table to default values.

What gets reset:
  - conf_thresh → 0.55  (default)
  - risk_factor → 1.00  (default)
  - Q-table     → {}    (cleared — agent relearns from scratch)

What is preserved:
  - n_updates  (epsilon decay level is kept so exploration
                doesn't jump back to 15% after thousands of trades)

Usage (from workspace root, venv activated):
    python ai/reset_scalping_rl.py

    Optional flags:
    --mode live     Reset the live Q-table  (default: paper)
    --mode paper    Reset the paper Q-table (default)
    --both          Reset both paper AND live Q-tables
    --keep-qtable   Reset conf/risk only, keep learned Q-values
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger

# ── Constants (must match rl_agent.py) ───────────────────────────────────────
DATA_DIR            = ROOT / "ai" / "data"
DEFAULT_CONF_THRESH = 0.55
DEFAULT_RISK_FACTOR = 1.00


def reset_scalping_rl(mode: str, keep_qtable: bool = False) -> bool:
    """
    Reset scalping RL Q-table for a given account mode.
    Returns True on success, False on failure.
    """
    path = DATA_DIR / f"rl_qtable_scalping_{mode}.json"

    if not path.exists():
        logger.info(
            f"  {path.name} not found — "
            "agent will initialise with defaults on next startup"
        )
        return True

    try:
        data       = json.loads(path.read_text(encoding="utf-8"))
        n_updates  = data.get("n_updates", 0)
        old_conf   = data.get("conf_thresh", DEFAULT_CONF_THRESH)
        old_risk   = data.get("risk_factor", DEFAULT_RISK_FACTOR)
        old_states = len(data.get("q", {}))

        reset_data = {
            "q":           data.get("q", {}) if keep_qtable else {},
            "conf_thresh": DEFAULT_CONF_THRESH,
            "risk_factor": DEFAULT_RISK_FACTOR,
            "last_state":  None,
            "last_action": None,
            "n_updates":   n_updates,
        }

        # Write atomically — write to temp then rename
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(reset_data, indent=2), encoding="utf-8")
        tmp.rename(path)

        logger.info(f"  ✓ scalping/{mode} reset:")
        logger.info(f"      conf_thresh : {old_conf:.4f} → {DEFAULT_CONF_THRESH:.4f}")
        logger.info(f"      risk_factor : {old_risk:.4f} → {DEFAULT_RISK_FACTOR:.4f}")
        logger.info(f"      Q-table     : {old_states} states → {'kept' if keep_qtable else 'cleared'}")
        logger.info(f"      n_updates   : {n_updates} (preserved)")
        return True

    except Exception as exc:
        logger.error(f"  ✗ reset failed for scalping/{mode}: {exc}")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset scalping RL Q-table")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Which account mode Q-table to reset (default: paper)",
    )
    parser.add_argument(
        "--both",
        action="store_true",
        help="Reset both paper AND live Q-tables",
    )
    parser.add_argument(
        "--keep-qtable",
        action="store_true",
        help="Reset conf_thresh and risk_factor only — keep learned Q-values",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Scalping RL Agent Reset")
    logger.info("=" * 60)

    if not DATA_DIR.exists():
        logger.error(f"Data directory not found: {DATA_DIR}")
        logger.error("Make sure you run this from the project root with venv active.")
        sys.exit(1)

    modes = ["paper", "live"] if args.both else [args.mode]

    all_ok = True
    for mode in modes:
        logger.info(f"\nResetting scalping RL — mode: {mode}")
        ok = reset_scalping_rl(mode, keep_qtable=args.keep_qtable)
        if not ok:
            all_ok = False

    logger.info("\n" + "=" * 60)
    if all_ok:
        logger.info("Reset complete.")
        logger.info("Restart the backend for changes to take effect.")
    else:
        logger.error("Reset completed with errors — check output above.")
        sys.exit(1)


if __name__ == "__main__":
    main()