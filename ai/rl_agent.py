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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Q-learning hyper-parameters
ALPHA         = 0.1    # learning rate
GAMMA         = 0.9    # discount factor
# Exploration: decays from EPSILON_START toward EPSILON_MIN as the agent gains experience
EPSILON_START = 0.15   # initial exploration rate (15% random actions)
EPSILON_MIN   = 0.02   # floor — always keep 2% exploration for non-stationarity
EPSILON_DECAY = 0.995  # per-update multiplier (reaches ~5% after ~250 trades)

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


def _session_bucket() -> str:
    """Return which Forex session the current UTC hour falls in."""
    h = datetime.now(tz=timezone.utc).hour
    if 12 <= h <= 17:
        return "overlap"   # London/NY overlap (highest volume)
    if 7 <= h < 22:
        return "active"    # London or New York open
    return "quiet"         # Asian / off-hours


def _drawdown_bucket(drawdown_pct: float) -> str:
    """Bucket current daily drawdown so RL can de-risk near the circuit-breaker."""
    if drawdown_pct >= 3.0:
        return "high"   # ≥ 3% — near the typical 5% daily limit
    if drawdown_pct >= 1.5:
        return "med"    # 1.5–3% — elevated caution
    return "low"        # < 1.5% — normal operating range


def _vol_bucket(vol_pct: float) -> str:
    """
    Volatility bucket derived from the SL-distance as a percentage of entry price.
    Since SL is typically set as N×ATR from entry, this is a cheap ATR proxy
    available at trade-close time without an extra OHLCV fetch.

    Thresholds:
      < 1.0%  → tight  (typical forex / index scalp in calm market)
      ≥ 1.0%  → wide   (crypto, gold, oil, or a high-volatility forex session)
    """
    return "wide" if vol_pct >= 1.0 else "tight"


def _state(win_rate: float, avg_conf: float, drawdown_pct: float = 0.0, vol_pct: float = 0.0) -> str:
    """162-state space: wr × conf × session × drawdown × volatility (3×3×3×3×2)."""
    return (
        f"{_wr_bucket(win_rate)}_{_conf_bucket(avg_conf)}"
        f"_{_session_bucket()}_{_drawdown_bucket(drawdown_pct)}_{_vol_bucket(vol_pct)}"
    )


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

    def __init__(self, trading_type: str, mode: str = "live"):
        self.trading_type = trading_type
        self._mode        = mode   # "live" or "paper"
        self._lock        = threading.Lock()

        self._q: dict[str, list[float]] = {}   # state → Q-values for each action
        self._conf_thresh = DEFAULT_CONF_THRESH
        self._risk_factor = DEFAULT_RISK_FACTOR
        self._last_state:  Optional[str] = None
        self._last_action: Optional[int] = None
        self._n_updates:   int            = 0  # total observe() calls — drives epsilon decay

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

    def observe(self, win_rate: float, avg_conf: float, reward: float, drawdown_pct: float = 0.0, vol_pct: float = 0.0) -> None:
        """
        Called after a trade closes. Updates Q-table based on outcome.
        reward = profit_pct (positive = win, negative = loss).
        Reward is clipped to [-0.10, 0.10] to prevent large single-trade
        spikes (e.g. news events, gold volatility) from distorting Q-values.
        drawdown_pct = current daily drawdown % (0.0 = fresh day, 4.0 = 4% down).
        vol_pct      = SL-distance as % of entry price (ATR proxy for volatility regime).
        """
        with self._lock:
            # Normalise reward: clip extreme values so Q-table stays stable
            reward = max(-0.10, min(0.10, float(reward)))
            self._n_updates += 1
            new_state = _state(win_rate, avg_conf, drawdown_pct, vol_pct)
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
        current_eps = max(EPSILON_MIN, EPSILON_START * (EPSILON_DECAY ** self._n_updates))
        return {
            "trading_type":         self.trading_type,
            "mode":                 self._mode,
            "confidence_threshold": round(self._conf_thresh, 4),
            "risk_factor":          round(self._risk_factor, 4),
            "q_states":             len(self._q),
            "last_state":           self._last_state,
            "n_updates":            self._n_updates,
            "epsilon":              round(current_eps, 4),
        }

    # ── internal ──────────────────────────────────────────────────────────────

    def _choose_action(self, state: str) -> int:
        """Epsilon-greedy action selection with exponential decay."""
        current_eps = max(EPSILON_MIN, EPSILON_START * (EPSILON_DECAY ** self._n_updates))
        if random.random() < current_eps:
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
        path = DATA_DIR / f"rl_qtable_{self.trading_type}_{self._mode}.json"
        payload = {
            "q":            self._q,
            "conf_thresh":  self._conf_thresh,
            "risk_factor":  self._risk_factor,
            "last_state":   self._last_state,
            "last_action":  self._last_action,
            "n_updates":    self._n_updates,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    def _load(self) -> None:
        path = DATA_DIR / f"rl_qtable_{self.trading_type}_{self._mode}.json"
        # Migrate old filename (no mode suffix) to new name on first run
        if not path.exists():
            legacy = DATA_DIR / f"rl_qtable_{self.trading_type}.json"
            if legacy.exists():
                try:
                    legacy.rename(path)
                    logger.info(f"RL: migrated {legacy.name} -> {path.name}")
                except Exception:
                    pass
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
            self._n_updates   = data.get("n_updates", 0)
            logger.info(
                f"RL agent loaded [{self.trading_type}/{self._mode}]: "
                f"conf_thresh={self._conf_thresh:.2f} "
                f"risk_factor={self._risk_factor:.2f}"
            )
        except Exception as exc:
            logger.warning(f"RL agent could not load [{self.trading_type}/{self._mode}]: {exc}")


class RLAgentManager:
    """
    Holds one RLAgent per trading type. Each account mode (live/paper)
    gets its own Q-tables so they learn independently.
    """

    def __init__(self):
        # Read current account mode at startup so the right Q-tables are loaded
        try:
            from engine.account_store import current_mode as _cm
            _mode = _cm()
        except Exception:
            _mode = "live"
        self._mode = _mode
        self._agents = {
            tt: RLAgent(tt, mode=_mode)
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
        drawdown_pct: float = 0.0,
        vol_pct:      float = 0.0,
    ) -> None:
        """Feed a closed trade result into the appropriate RL agent."""
        self.agent(trading_type).observe(win_rate, avg_conf, profit_pct, drawdown_pct, vol_pct)

    def status(self) -> dict:
        return {tt: ag.status() for tt, ag in self._agents.items()}


# Application-level singleton
rl_manager = RLAgentManager()
