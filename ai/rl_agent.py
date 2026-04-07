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

# Per-mode learning rates — scalping needs faster adaptation (intraday regime shifts),
# swing needs slower adaptation (multi-day trends require more confirmation).
# ALPHA is the base rate; these scale it per trading type.
_ALPHA_BY_MODE: dict[str, float] = {
    "scalping":    0.15,   # faster — M5 regimes change intraday
    "day_trading": 0.10,   # default — balanced H1 adaptation
    "swing":       0.05,   # slower — H4/D1 trends need multi-day confirmation
}

# Parameter bounds
CONF_MIN, CONF_MAX   = 0.40, 0.85
RISK_MIN, RISK_MAX   = 0.50, 1.50
CONF_STEP            = 0.02
RISK_STEP            = 0.05

# Default starting values
DEFAULT_CONF_THRESH  = 0.55
DEFAULT_RISK_FACTOR  = 1.00
_CONF_MAX_BY_MODE: dict[str, float] = {
    "scalping":    0.72,   # M5 — you've seen 68%, 72% gives headroom
    "day_trading": 0.78,   # H1 — slightly higher, more reliable LSTM
    "swing":       0.78,   # H4 — same as day trading
}

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
        # Initialize to startup time so idle decay can measure elapsed time
        # from agent load, not from the first trade close. Without this,
        # getattr(agent, '_last_update_ts', _now_ts) always returns _now_ts
        # -> idle_hours = 0 -> decay never fires on a fresh account with no trades.
        self._last_update_ts: float = datetime.now(tz=timezone.utc).timestamp()

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
            # Idle decay: if conf_thresh is above default and no trade
            # has been seen for 6+ hours, drift back by one step.
            # Prevents permanent deadlock if the threshold gets stuck
            # near the ceiling with no signals passing to generate trades.
            _now_ts  = datetime.now(tz=timezone.utc).timestamp()
            _last_ts = getattr(self, "_last_update_ts", _now_ts)
            
            # Idle decay speed is mode-aware: scalping decays every 4h (fast reset),
            # swing decays every 12h (slow — swing positions can be open for days).
            _IDLE_DECAY_HOURS = {"scalping": 4.0, "day_trading": 6.0, "swing": 12.0}
            _idle_threshold = _IDLE_DECAY_HOURS.get(self.trading_type, 6.0)
            if (_now_ts - _last_ts) / 3600 >= _idle_threshold and self._conf_thresh > DEFAULT_CONF_THRESH:
                self._conf_thresh = max(DEFAULT_CONF_THRESH, self._conf_thresh - CONF_STEP)
                logger.info(
                    f"RL [{self.trading_type}] idle decay: "
                    f"conf_thresh → {self._conf_thresh:.2f}"
                )
            self._last_update_ts = _now_ts
          
            # Normalise reward: asymmetric clip — tighter floor captures moderate losses
            # more precisely; looser ceiling lets strong wins register clearly.
            # MATH-3: was ±0.10 (symmetric) — extreme losses clipped same as moderate.
            reward = max(-0.05, min(0.15, float(reward)))
            # Behavioral guardrail: penalize low win-rate states to prevent
            # the agent from learning "take everything to recover losses" (gambler's fallacy).
            # When win_rate < 40%, subtract a small penalty so the Q-table associates
            # low-WR states with negative value — pushing conf_thresh UP, not down.
            if win_rate < 0.40:
                reward -= 0.02  # nudge Q-values toward more selective behavior
            elif win_rate < 0.45:
                reward -= 0.01  # lighter nudge in marginal zone
                
            self._n_updates += 1
            new_state = _state(win_rate, avg_conf, drawdown_pct, vol_pct)
            self._update_q(new_state, reward)
            action_idx = self._choose_action(new_state)
            conf_delta, risk_delta = ACTIONS[action_idx]
            _conf_max = _CONF_MAX_BY_MODE.get(self.trading_type, CONF_MAX)
            # Floor: never allow conf_thresh to drop below mode-specific safe minimum.
            # Without this, Q-learning on a bad win-rate sequence can drive
            # conf_thresh to 0.40 (CONF_MIN) which causes overtrade/blowup.
            _CONF_FLOOR_BY_MODE = {
                "scalping":    0.52,
                "day_trading": 0.52,
                "swing":       0.50,
            }
            _conf_floor = _CONF_FLOOR_BY_MODE.get(self.trading_type, 0.52)
            self._conf_thresh = float(
                max(_conf_floor, min(_conf_max, self._conf_thresh + conf_delta))
            )
            # Risk factor floor: never drop below 0.60 — below this,
            # lot sizes become so small they barely cover spread cost.
            _RF_FLOOR_BY_MODE = {
                "scalping":    0.60,
                "day_trading": 0.60,
                "swing":       0.60,
            }
            _rf_floor = _RF_FLOOR_BY_MODE.get(self.trading_type, 0.60)
            self._risk_factor = float(
                max(_rf_floor, min(RISK_MAX, self._risk_factor + risk_delta))
            )

            self._last_state  = new_state
            self._last_action = action_idx
            # Snapshot payload to save outside the lock (avoid holding lock during I/O)
            _save_payload = {
                "q":            dict(self._q),
                "conf_thresh":  self._conf_thresh,
                "risk_factor":  self._risk_factor,
                "last_state":   self._last_state,
                "last_action":  self._last_action,
                "n_updates":    self._n_updates,
            }
            _save_path = DATA_DIR / f"rl_qtable_{self.trading_type}_{self._mode}.json"
        # Disk write happens outside the lock to avoid blocking concurrent reads.
        # IMPROVE-1: only write every 10 updates to reduce I/O on busy sessions.
        if self._n_updates % 10 == 0:
            try:
                with open(_save_path, "w", encoding="utf-8") as f:
                    json.dump(_save_payload, f)
            except Exception as exc:
                logger.warning(f"RL save failed [{self.trading_type}]: {exc}")
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

    def shutdown(self) -> None:
        """Force-save Q-table to disk — call on application shutdown to avoid
        losing up to 9 updates that accumulate between periodic saves."""
        self._force_save()
        logger.info(f"RL agent saved on shutdown [{self.trading_type}/{self._mode}]")

    # ── internal ──────────────────────────────────────────────────────────────

    _HOLD_ACTION = 4  # index of (0.0, 0.0) in ACTIONS — hold both parameters

    def _force_save(self) -> None:
        """Write the current Q-table and parameters to disk unconditionally."""
        with self._lock:
            _payload = {
                "q":           dict(self._q),
                "conf_thresh": self._conf_thresh,
                "risk_factor": self._risk_factor,
                "last_state":  self._last_state,
                "last_action": self._last_action,
                "n_updates":   self._n_updates,
            }
        _path = DATA_DIR / f"rl_qtable_{self.trading_type}_{self._mode}.json"
        try:
            with open(_path, "w", encoding="utf-8") as f:
                json.dump(_payload, f)
        except Exception as exc:
            logger.warning(f"RL force-save failed [{self.trading_type}/{self._mode}]: {exc}")

    def _choose_action(self, state: str) -> int:
        """Epsilon-greedy action selection with exponential decay.
        Ties (including all-zero untrained states) break toward hold so that
        a brand-new agent doesn't blindly decrease parameters on its first update.
        """
        current_eps = max(EPSILON_MIN, EPSILON_START * (EPSILON_DECAY ** self._n_updates))
        if random.random() < current_eps:
            return random.randrange(len(ACTIONS))
        q_vals = self._q.get(state, [0.0] * len(ACTIONS))
        best_q = max(q_vals)
        best_indices = [i for i, v in enumerate(q_vals) if v == best_q]
        # Prefer hold when multiple actions are tied (especially the all-zero initial state)
        return self._HOLD_ACTION if self._HOLD_ACTION in best_indices else best_indices[0]

    def _update_q(self, new_state: str, reward: float) -> None:
        """Bellman update for the previous (state, action) pair.
        Uses per-mode learning rate so scalping adapts faster than swing."""
        if self._last_state is None or self._last_action is None:
            return
        _alpha = _ALPHA_BY_MODE.get(self.trading_type, ALPHA)
        prev = self._q.setdefault(self._last_state, [0.0] * len(ACTIONS))
        next_max = max(self._q.get(new_state, [0.0] * len(ACTIONS)))
        prev[self._last_action] += _alpha * (
            reward + GAMMA * next_max - prev[self._last_action]
        )

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
            # Bootstrap live agent from paper learning on first live run.
            # Paper uses identical live market data (same prices, spreads, volatility),
            # so everything the agent learned in paper is directly applicable to live.
            # This means the full Q-table, conf_thresh, and risk_factor all carry over.
            if self._mode == "live":
                paper_path = DATA_DIR / f"rl_qtable_{self.trading_type}_paper.json"
                if paper_path.exists():
                    try:
                        with open(paper_path, "r", encoding="utf-8") as f:
                            paper_data = json.load(f)
                        self._q           = paper_data.get("q", {})
                        self._last_state  = paper_data.get("last_state")
                        self._last_action = paper_data.get("last_action")
                        self._n_updates   = paper_data.get("n_updates", 0)
                        _conf_max_boot = _CONF_MAX_BY_MODE.get(self.trading_type, CONF_MAX)
                        _CONF_FLOOR_BOOT = {"scalping": 0.52, "day_trading": 0.52, "swing": 0.50}
                        _conf_floor_boot = _CONF_FLOOR_BOOT.get(self.trading_type, 0.52)
                        _RF_FLOOR_BOOT = {"scalping": 0.60, "day_trading": 0.60, "swing": 0.60}
                        _rf_floor_boot = _RF_FLOOR_BOOT.get(self.trading_type, 0.60)
                        self._conf_thresh = float(
                            max(_conf_floor_boot, min(
                                paper_data.get("conf_thresh", DEFAULT_CONF_THRESH),
                                _conf_max_boot
                            ))
                        )
                        self._risk_factor = float(
                            max(_rf_floor_boot, min(
                                paper_data.get("risk_factor", DEFAULT_RISK_FACTOR),
                                RISK_MAX
                            ))
                        )
                        logger.info(
                            f"RL live agent bootstrapped from paper [{self.trading_type}]: "
                            f"conf_thresh={self._conf_thresh:.2f} "
                            f"risk_factor={self._risk_factor:.2f} "
                            f"n_updates={self._n_updates}"
                        )
                    except Exception as exc:
                        logger.warning(f"RL: could not bootstrap live from paper [{self.trading_type}]: {exc}")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._q           = data.get("q", {})
            self._last_state  = data.get("last_state")
            self._last_action = data.get("last_action")
            self._n_updates   = data.get("n_updates", 0)
            # Enforce per-mode cap on load — prevents a previously saved value
            # that exceeded the new cap (e.g. 0.85 scalping) from bypassing it.
            _conf_max_load = _CONF_MAX_BY_MODE.get(self.trading_type, CONF_MAX)
            _CONF_FLOOR_LOAD = {"scalping": 0.52, "day_trading": 0.52, "swing": 0.50}
            _conf_floor_load = _CONF_FLOOR_LOAD.get(self.trading_type, 0.52)
            _RF_FLOOR_LOAD = {"scalping": 0.60, "day_trading": 0.60, "swing": 0.60}
            _rf_floor_load = _RF_FLOOR_LOAD.get(self.trading_type, 0.60)
            self._conf_thresh = float(
                max(_conf_floor_load, min(data.get("conf_thresh", DEFAULT_CONF_THRESH), _conf_max_load))
            )
            self._risk_factor = float(
                max(_rf_floor_load, min(data.get("risk_factor", DEFAULT_RISK_FACTOR), RISK_MAX))
            )

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

    def switch_mode(self, new_mode: str) -> None:
        """Reload all RL agents for a new account mode (live ↔ paper).
        Called by the account route after a successful mode switch so the correct
        Q-tables are loaded and live/paper learning remains isolated.
        """
        self._mode = new_mode
        self._agents = {
            tt: RLAgent(tt, mode=new_mode)
            for tt in ("scalping", "day_trading", "swing")
        }
        logger.info(f"RL agents reloaded for mode: {new_mode}")

    def should_take_signal(self, trading_type: str, confidence: float) -> bool:
        return self.agent(trading_type).should_take_signal(confidence)

    def risk_factor(self, trading_type: str) -> float:
        return self.agent(trading_type).risk_factor

    def shutdown(self) -> None:
        """Force-save all Q-tables — called from API lifespan shutdown so that
        up to 9 pending updates are not lost on ungraceful termination."""
        for ag in self._agents.values():
            ag.shutdown()

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