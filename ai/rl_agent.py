"""
RL Agent — learns from closed trade outcomes to tune the confidence
threshold and risk% per strategy over time.

Design: lightweight tabular Q-learning (no neural net needed here —
the LSTM already does the heavy lifting). The RL agent adjusts two
parameters per (strategy, trading_type) pair:

  1. confidence_threshold  — minimum signal confidence to allow entry
     (starts at 0.55, adjusts between 0.40–0.85)

  2. risk_factor           — multiplier on the base risk % from risk.json
     (starts at 1.0, adjusts between 0.5–1.5)

State: (recent_win_rate_bucket, avg_confidence_bucket)
  win_rate buckets:   low (<40%), medium (40–60%), high (>60%)
  avg_conf buckets:   low (<0.55), medium (0.55–0.70), high (>0.70)
  → 9 states total

Actions per parameter:
  confidence_threshold: decrease | hold | increase  (3)
  risk_factor:          decrease | hold | increase  (3)
  → 9 joint actions

Reward: profit_pct of the most recent trade.

Q-table persisted to: ai/data/rl_qtable_{trading_type}.json
"""

from __future__ import annotations

import json
import random
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Q-learning hyper-parameters
ALPHA       = 0.1    # learning rate
GAMMA       = 0.9    # discount factor
EPSILON     = 0.15   # exploration rate (fixed — no decay needed for this use)

# Parameter bounds
CONF_MIN, CONF_MAX   = 0.40, 0.85
RISK_MIN, RISK_MAX   = 0.50, 1.50
CONF_STEP            = 0.02
RISK_STEP            = 0.05

# Default starting values
DEFAULT_CONF_THRESH  = 0.55
DEFAULT_RISK_FACTOR  = 1.00


def _wr_bucket(win_rate: float) -> str:
    if win_rate < 0.40:
        return "low"
    if win_rate <= 0.60:
        return "med"
    return "high"


def _conf_bucket(avg_conf: float) -> str:
    if avg_conf < 0.55:
        return "low"
    if avg_conf <= 0.70:
        return "med"
    return "high"


def _state(win_rate: float, avg_conf: float) -> str:
    return f"{_wr_bucket(win_rate)}_{_conf_bucket(avg_conf)}"


# Joint actions: (conf_delta, risk_delta)
ACTIONS: list[tuple[float, float]] = [
    (-CONF_STEP, -RISK_STEP),
    (-CONF_STEP,  0.0),
    (-CONF_STEP, +RISK_STEP),
    (0.0,        -RISK_STEP),
    (0.0,         0.0),        # hold
    (0.0,        +RISK_STEP),
    (+CONF_STEP, -RISK_STEP),
    (+CONF_STEP,  0.0),
    (+CONF_STEP, +RISK_STEP),
]


class RLAgent:
    """
    Per-trading-type Q-learning agent that tunes confidence threshold
    and risk factor based on recent trade outcomes.
    """

    def __init__(self, trading_type: str):
        self.trading_type = trading_type
        self._lock        = threading.Lock()

        self._q: dict[str, list[float]] = {}   # state → Q-values for each action
        self._conf_thresh = DEFAULT_CONF_THRESH
        self._risk_factor = DEFAULT_RISK_FACTOR
        self._last_state:  Optional[str] = None
        self._last_action: Optional[int] = None

        self._load()

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def confidence_threshold(self) -> float:
        return self._conf_thresh

    @property
    def risk_factor(self) -> float:
        return self._risk_factor

    def should_take_signal(self, confidence: float) -> bool:
        """Return True if signal confidence meets the learned threshold."""
        return confidence >= self._conf_thresh

    def observe(self, win_rate: float, avg_conf: float, reward: float) -> None:
        """
        Called after a trade closes. Updates Q-table based on outcome.
        reward = profit_pct (positive = win, negative = loss).
        """
        with self._lock:
            new_state = _state(win_rate, avg_conf)
            self._update_q(new_state, reward)
            action_idx = self._choose_action(new_state)
            conf_delta, risk_delta = ACTIONS[action_idx]
            self._conf_thresh = float(
                max(CONF_MIN, min(CONF_MAX, self._conf_thresh + conf_delta))
            )
            self._risk_factor = float(
                max(RISK_MIN, min(RISK_MAX, self._risk_factor + risk_delta))
            )
            self._last_state  = new_state
            self._last_action = action_idx
            self._save()
            logger.debug(
                f"RL [{self.trading_type}] reward={reward:+.4f} "
                f"conf_thresh={self._conf_thresh:.2f} "
                f"risk_factor={self._risk_factor:.2f}"
            )

    def status(self) -> dict:
        return {
            "trading_type":        self.trading_type,
            "confidence_threshold": round(self._conf_thresh, 4),
            "risk_factor":          round(self._risk_factor, 4),
            "q_states":             len(self._q),
            "last_state":           self._last_state,
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _choose_action(self, state: str) -> int:
        """Epsilon-greedy action selection."""
        if random.random() < EPSILON:
            return random.randrange(len(ACTIONS))
        q_vals = self._q.get(state, [0.0] * len(ACTIONS))
        return int(max(range(len(ACTIONS)), key=lambda i: q_vals[i]))

    def _update_q(self, new_state: str, reward: float) -> None:
        """Bellman update for the previous (state, action) pair."""
        if self._last_state is None or self._last_action is None:
            return
        prev = self._q.setdefault(self._last_state, [0.0] * len(ACTIONS))
        next_max = max(self._q.get(new_state, [0.0] * len(ACTIONS)))
        prev[self._last_action] += ALPHA * (
            reward + GAMMA * next_max - prev[self._last_action]
        )

    def _save(self) -> None:
        path = DATA_DIR / f"rl_qtable_{self.trading_type}.json"
        payload = {
            "q":            self._q,
            "conf_thresh":  self._conf_thresh,
            "risk_factor":  self._risk_factor,
            "last_state":   self._last_state,
            "last_action":  self._last_action,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _load(self) -> None:
        path = DATA_DIR / f"rl_qtable_{self.trading_type}.json"
        if not path.exists():
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._q           = data.get("q", {})
            self._conf_thresh = data.get("conf_thresh", DEFAULT_CONF_THRESH)
            self._risk_factor = data.get("risk_factor", DEFAULT_RISK_FACTOR)
            self._last_state  = data.get("last_state")
            self._last_action = data.get("last_action")
            logger.info(
                f"RL agent loaded [{self.trading_type}]: "
                f"conf_thresh={self._conf_thresh:.2f} "
                f"risk_factor={self._risk_factor:.2f}"
            )
        except Exception as exc:
            logger.warning(f"RL agent could not load [{self.trading_type}]: {exc}")


class RLAgentManager:
    """
    Holds one RLAgent per trading type and provides trade-outcome-driven
    learning across all three modes from a single API.
    """

    def __init__(self):
        self._agents = {
            tt: RLAgent(tt)
            for tt in ("scalping", "day_trading", "swing")
        }

    def agent(self, trading_type: str) -> RLAgent:
        return self._agents.get(trading_type, self._agents["day_trading"])

    def should_take_signal(self, trading_type: str, confidence: float) -> bool:
        return self.agent(trading_type).should_take_signal(confidence)

    def risk_factor(self, trading_type: str) -> float:
        return self.agent(trading_type).risk_factor

    def on_trade_closed(
        self,
        trading_type: str,
        profit_pct:   float,
        win_rate:     float,
        avg_conf:     float,
    ) -> None:
        """Feed a closed trade result into the appropriate RL agent."""
        self.agent(trading_type).observe(win_rate, avg_conf, profit_pct)

    def status(self) -> dict:
        return {tt: ag.status() for tt, ag in self._agents.items()}


# Application-level singleton
rl_manager = RLAgentManager()
