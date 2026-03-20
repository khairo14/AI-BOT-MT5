# AI / ML System — Full Capabilities & Limits

This document describes exactly what each AI component does, its current limits, and how it is wired into the trading pipeline.

---

## Component Overview

| Component | Purpose | Runs when | Updates when |
|---|---|---|---|
| **LSTM Predictor** | Directional probability (0–1) per symbol×mode | Every signal check | Every 20th closed live/paper trade (auto), or manual Retrain |
| **Signal Scorer** | Blend LSTM + R:R + Trend + Volume → confidence | Every signal check | Weights are fixed; LSTM sub-score updates via retraining |
| **RL Agent** | Learn confidence threshold + risk factor per mode | Every signal check (gate) | After every closed trade |
| **Param Optimizer** | Find best strategy params via walk-forward grid search | On-demand, or auto when WR drops below 45% | After each optimizer run |
| **Trade Memory** | Central outcome log; source of truth for RL + optimizer | Always (every closed trade) | Appended after each close |

---

## 1. LSTM Price Predictor (`ai/predictor.py`)

**What it predicts:** Whether price will go up or down on the next bar. Returns a probability between 0 and 1 — `0.5 = neutral`, `>0.65 = strong directional signal`.

**Architecture:**
- 2-layer LSTM with hidden size 64
- Sequence length: 60 bars (the model sees the past 60 bars before making a call)
- Features per bar: `open, high, low, close, volume, returns, hl_range, oc_range`
- Output: binary classification (up vs down) — not a price regression
- Trained with `BCELoss` for 20 epochs with an 80/20 train/val split

**One model per symbol × trading type.** A `EURUSD` scalping model trains on M5 OHLCV. An `EURUSD` day trading model trains on H1. These are completely independent files stored as `ai/models/{symbol}_{type}_lstm.pt` + `{symbol}_{type}_scaler.pkl`.

**Timeframes used for training data:**

| Mode | Training TF |
|---|---|
| Scalping | M5 |
| Day Trading | H1 |
| Swing | H4 |

**When it retrains:**
- **Auto:** every 20th closed live/paper trade per symbol×mode (`signal_bus._poll_outcome`)
- **Manual:** "Retrain" or "Retrain All" buttons on the AI/ML Brain page

**Minimum data to train:** 70+ bars required (60 for sequence + warmup). If fewer bars are available, training is skipped.

**Hard limits:**
- CPU-only. Training takes a few seconds per model on modern hardware.
- No GPU acceleration — PyTorch is installed without CUDA.
- Predicts direction only — no price target, no confidence interval.
- Does not account for news events, session boundaries, or correlations between symbols.

---

## 2. Signal Scorer (`ai/signal_scorer.py`)

**What it produces:** A single 0–1 confidence score per signal, blending four components.

**Weights:**

| Component | Weight | Source |
|---|---|---|
| LSTM direction probability | 40% | `ai/predictor.py` |
| Risk:Reward ratio quality | 25% | Derived from entry/SL/TP |
| Trend alignment (EMA50 vs EMA200) | 20% | Computed from `df` |
| Volume confirmation | 15% | Last bar vs 20-bar avg |

**Score thresholds (for badge colours in the UI):**
- `≥ 0.75` → High confidence (green)
- `0.50–0.74` → Medium (yellow)
- `< 0.50` → Low (gray)

**Graceful degradation:** If LSTM is untrained or throws an error, LSTM component returns `0.5` (neutral) rather than crashing the signal. The overall score still reflects R:R, trend, and volume.

**What it does NOT do:**
- It does not block signals by itself. Scoring is informational until a gate checks the score.
- It does not learn or adapt — the weights are fixed constants.

---

## 3. Confidence Gates (two separate mechanisms)

Signals pass through **two independent gates** in `engine/strategy_runner.py`:

### Gate 1 — Static Threshold (configurable, off by default)

Controlled by `config/app.json → ai.confidence_filter_enabled`.

```json
"ai": {
  "confidence_filter_enabled": false,
  "confidence_threshold": 60
}
```

- When `false` (default): no filtering — all scored signals proceed regardless of confidence.
- When `true`: any signal with `confidence < confidence_threshold / 100` is blocked and logged as `"AI confidence gate blocked"`.
- `confidence_threshold` is in percent (e.g., `60` means block < 0.60).

**This is what "off by default" means** — signals are not filtered by a static threshold unless you enable it in Settings.

### Gate 2 — RL Dynamic Gate (always active)

Controlled by the RL agent's learned threshold. The RL agent starts at `0.55` (55%) and adjusts between `0.40` and `0.85` based on recent trade outcomes.

```python
if not scorer.is_tradeable(strat_sig.confidence, trading_type):
    return None  # signal suppressed
```

This gate is **always evaluated**, starting at day 1 with the 55% default. As the RL agent learns from closed trades, it shifts the threshold up or down (±0.02 per update).

> **Note:** `app.json → ai.rl_agent_enabled` and `ai.price_prediction_enabled` appear in the config file but are not currently wired into the execution path. The scorer and RL gate always run regardless of these flags. These flags are reserved for future configurability.

---

## 4. RL Agent (`ai/rl_agent.py`)

**Algorithm:** Tabular Q-learning (no neural net).

**What it controls:**
1. `confidence_threshold` — signals below this are suppressed (Gate 2 above)
2. `risk_factor` — multiplier on the base risk% from `config/risk.json`

**State space (9 states):**

| | Conf: Low (<0.55) | Conf: Med (0.55–0.70) | Conf: High (>0.70) |
|---|---|---|---|
| **WR: Low (<40%)** | `low_low` | `low_med` | `low_high` |
| **WR: Med (40–60%)** | `med_low` | `med_med` | `med_high` |
| **WR: High (>60%)** | `high_low` | `high_med` | `high_high` |

**Action space (9 joint actions):**
Every combination of: `{decrease, hold, increase}` for `conf_threshold` × `{decrease, hold, increase}` for `risk_factor`.

Steps: `conf ±0.02`, `risk ±0.05`.

**Update rule:** Bellman equation, `ALPHA=0.1`, `GAMMA=0.9`. Reward = `profit_pct` of the closed trade.

**Exploration:** 15% random actions (`EPSILON=0.15`) — permanently fixed, no decay.

**Persistence:** Q-tables saved to `ai/data/rl_qtable_{scalping|day_trading|swing}.json` after every update.

**Hard limits:**
- Operates only on the last ~50 trades from memory for win-rate calculation (inside `trade_memory.stats()`).
- Does not consider session time, news, or volatility regime — just win rate + average confidence.
- Cannot increase `confidence_threshold` above `0.85` or `risk_factor` above `1.5`.
- One agent per trading type — scalping/day_trading/swing RL tables are independent.

---

## 5. Parameter Optimizer (`ai/param_optimizer.py`)

**What it optimizes:** Strategy-level entry parameters (e.g., EMA periods, RSI threshold, BB std dev). These affect signal generation, not trade sizing or risk.

**Method:** Walk-forward grid search
1. Iterates over every parameter combination in the grid (up to `MAX_GRID_COMBOS = 64`)
2. For each combo: runs a bar-by-bar simulation on historical OHLCV (no look-ahead)
3. Scores each combo as `win_rate × avg_risk_reward`
4. Saves the best combo to `config/optimized_params.json`

**Walk-forward step sizes by mode:**

| Mode | Step (every N bars) | Max hold | Warmup |
|---|---|---|---|
| Scalping | 3 | 50 bars | 50 bars |
| Day Trading | 5 | 100 bars | 100 bars |
| Swing | 10 | 200 bars | 200 bars |

**Trigger conditions:**
1. **Manual**: "Run" or "Optimize All" buttons on the AI/ML Brain page
2. **Post-backtest auto**: If a user-run backtest produces ≥ 30 simulated trades, the optimizer is triggered on the same data
3. **Live auto**: When win rate for a strategy+symbol drops below `45%` (≥ 20 trades required, 24h cooldown)

**Cooldown:** 24 hours between automatic re-optimizations per strategy+symbol pair.

**Parameter grids (search space per strategy):**

| Strategy | Parameters | Combos |
|---|---|---|
| `ema_scalp` | ema_fast×4, ema_slow×4, rr×4 | 64 |
| `bb_squeeze` | bb_period×3, bb_std×3, min_squeeze_bars×3 | 27 (cap: 27) |
| `vwap_reversion` | sigma_entry×3, sigma_sl×3, rsi_period×3 | 27 |
| `macd_ema_trend` | macd_fast×2, macd_slow×2, macd_signal×2, ema_fast×2, ema_slow×2 | 32 |
| `sr_breakout` | lookback_bars×3, atr_period×3 | 9 |
| `rsi_divergence` | rsi_period×3, ema_bias_period×3 | 9 |
| `ema_trend_rider` | ema_fast×3, ema_slow×3 | 9 |
| `fibonacci_rsi` | rsi_period×3, fib_lookback×3 | 9 |
| `weekly_breakout` | lookback_bars×3, atr_mult_sl×3 | 9 |

**Hard limits:**
- Grid search is CPU-bound and synchronous within its background thread. Runs one job at a time per (strategy, symbol) pair.
- Minimum 5 signals fired per combo before it qualifies — low-signal combos are discarded.
- Does not simulate spread, slippage, or swap costs.
- Optimized params affect the strategy on the **next** run — no restart needed (re-read from `config/optimized_params.json` each cycle).

---

## 6. Trade Memory (`ai/trade_memory.py`)

**Central data store** for all closed trade outcomes.

**Sources:**

| Source | When written | Notes |
|---|---|---|
| Live trades | After MT5 position closes | Full data: confidence, pips, P&L |
| Paper trades | After MT5 position closes | Same as live — uses XM Demo |
| Backtest simulations | After each backtest run | `source="backtest"`, `confidence=0.5` |

**Storage:**
- Disk: `ai/data/trade_memory.jsonl` — append-only, unlimited size
- RAM: rolling buffer of last **5,000** entries for fast reads

**Key fields per entry:** `ticket`, `symbol`, `strategy`, `trading_type`, `direction`, `confidence`, `outcome` (tp_hit/sl_hit/manual_close/timeout), `profit_pct`, `extra.source`

**Consumers:**
- RL Agent — reads `stats()` after each closed trade for win-rate + avg-conf state
- Param Optimizer — reads `recent(n=50)` to check if auto-reoptimization should trigger
- LSTM auto-retrain — counted in `signal_bus` (every 20th trade per symbol×mode)
- Dashboard — "Trade Memory" section shows aggregate stats per mode

---

## 7. The Full Signal Lifecycle

```
MT5 bar arrives
    │
    ▼
Strategy generates raw signal (entry, SL, TP)
    │
    ▼
SignalScorer.score() produces confidence (0–1)
    │
    ├── LSTM (40%): predictor.predict(symbol, df, trading_type) → 0–1 probability
    ├── R:R (25%): reward/risk capped at 4:1
    ├── Trend (20%): EMA50 vs EMA200 alignment
    └── Volume (15%): last bar vs 20-bar avg
    │
    ▼
Gate 1 — Static filter (if confidence_filter_enabled=true):
    confidence < threshold → BLOCK signal
    │
    ▼
Gate 2 — RL dynamic gate (always active):
    confidence < rl_agent.confidence_threshold → BLOCK signal
    │
    ▼
Signal passed to execution pipeline (risk check → order submit)
    │
    ▼
Trade closes → TradeMemory.record() → RL.observe() → LSTM retrain counter
```

---

## 8. Configuration Reference

All AI flags live in `config/app.json` under the `"ai"` key:

| Key | Default | Effect |
|---|---|---|
| `price_prediction_enabled` | `false` | Reserved — not currently enforced in code |
| `confidence_filter_enabled` | `false` | When `true`: enables Gate 1 (static threshold block) |
| `confidence_threshold` | `60` | Gate 1 threshold in percent (60 = block below 0.60) |
| `rl_agent_enabled` | `false` | Reserved — not currently enforced; RL gate always runs |

**Settings page controls:** The AI/ML Brain page lets you manually trigger retraining and optimization. The circuit breaker (separate from AI) lives in Settings → Risk.

---

## 9. Current Constraints Summary

| Constraint | Value |
|---|---|
| LSTM min bars to train | 70 |
| LSTM sequence length | 60 bars |
| LSTM training epochs | 20 |
| LSTM GPU | None (CPU only) |
| RL confidence range | 0.40 – 0.85 |
| RL risk factor range | 0.50 – 1.50 |
| RL exploration rate | 15% (fixed) |
| Optimizer max grid combos | 64 |
| Optimizer min signals per combo | 5 |
| Optimizer cooldown | 24 hours |
| Auto-retrain trigger | every 20 closed trades per symbol×mode |
| Auto-optimizer trigger | win rate < 45% with ≥ 20 trades |
| Trade memory RAM buffer | last 5,000 entries |
