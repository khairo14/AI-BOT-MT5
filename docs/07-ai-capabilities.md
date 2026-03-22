# AI / ML System — Full Capabilities & Limits

This document describes exactly what each AI component does, its current limits, and how it is wired into the trading pipeline.

---

## Component Overview

| Component | Purpose | Runs when | Updates when |
|---|---|---|---|
| **LSTM Predictor** | Directional probability (0–1) per symbol×mode | Every signal check | Every 20th closed live/paper trade, or model stale >7 days, or 5 consecutive losses (auto), or manual Retrain |
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
- Input features per bar (**7 total**):
  - `close_return` — (close[i] − close[i−1]) / close[i−1]
  - `hl_range` — (high − low) / close
  - `oc_body` — (close − open) / open
  - `volume_norm` — volume / mean volume
  - `upper_wick` — (high − close) / close
  - `is_near_news` — per-bar flag: 1.0 only for bars within the 45-min news blackout window (scaled to bar count for the timeframe), else 0.0. This prevents the train/inference distribution shift that occurred when the whole 60-bar sequence received the same current news state.
  - `atr_norm` — ATR(14) / close: normalised volatility regime indicator — tells the model whether the market is in a high/low volatility environment
- Output: binary classification (up vs down) — not a price regression
- Trained with `BCELoss` for 20 epochs, gradient clipping `max_norm=1.0`, 80/20 train/val split

**Accuracy gate:** After training, if validation accuracy is below **52%** (statistically indistinguishable from random), the new weights are discarded and the previous model file is kept intact. This prevents a coin-flip model from overwriting a working one during low-data or adverse-regime retrains.

**One model per symbol × trading type.** A `EURUSD` scalping model trains on M5 OHLCV. An `EURUSD` day trading model trains on H1. These are completely independent files stored as `ai/models/{symbol}_{type}_lstm.pt` + `{symbol}_{type}_scaler.pkl`.

**Timeframes used for training data:**

| Mode | Training TF |
|---|---|
| Scalping | M5 |
| Day Trading | H1 |
| Swing | H4 |

**When it retrains (three triggers, whichever fires first):**
- **Trigger 1 — Trade count:** every 20th closed live/paper trade per symbol×mode
- **Trigger 2 — Staleness:** if the model is older than 7 days AND ≥ 10 trades exist for this symbol×mode
- **Trigger 3 — Consecutive losses:** if the last 5 closed trades for this symbol×mode all lost (regime-change indicator)
- **Manual:** "Retrain" or "Retrain All" buttons on the AI/ML Brain page

**Minimum data to train:** 70+ bars required (60 for sequence + warmup). If fewer bars are available, training is skipped.

**Hard limits:**
- Predicts direction only — no price target, no confidence interval.
- Does not account for news events or session boundaries directly (news is captured via `is_near_news` feature).
- GPU-accelerated when CUDA is available (auto-detected at runtime via `torch.cuda.is_available()`). Falls back to CPU if no CUDA device is found. Models are saved to CPU format and loaded to the appropriate device on each run.
- Changing `INPUT_SIZE` (e.g. adding features) invalidates existing `.pt` model files — they will auto-retrain on the next trigger.

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

**Trend score — graduated (not binary):** The EMA alignment component uses a continuous score instead of a hard 1.0/0.1 flip:
- `gap_pct = (EMA50 − EMA200) / |EMA200|` — measures how far apart the EMAs are as a percentage
- `trend_strength = clip(gap_pct / 0.02, −1, +1)` — normalises: ±2% gap = ±1
- `raw_score = 0.5 + 0.4 × trend_strength` — range **[0.1, 0.9]**
- BUY direction uses `raw_score` directly; SELL direction uses `1 − raw_score`
- A weak alignment (EMAs nearly equal) now correctly produces ~0.5, not a false 1.0

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

### Gate 2 — RL Dynamic Gate (controlled by `rl_agent_enabled`)

Controlled by the RL agent's learned threshold **and** the `config/app.json → ai.rl_agent_enabled` flag.

```json
"ai": {
  "rl_agent_enabled": true
}
```

- When `true` (default): RL gate is active — signals below the learned threshold are suppressed, and the RL risk_factor multiplier adjusts lot sizing.
- When `false`: gate bypassed — all signals pass through, lot sizing uses the raw risk.json value with no RL adjustment.

The RL agent starts at `0.55` (55%) and adjusts between `0.40` and `0.85` based on recent trade outcomes.

```python
if not scorer.is_tradeable(strat_sig.confidence, trading_type):
    return None  # signal suppressed
```

As the RL agent learns from closed trades, it shifts the threshold up or down (±0.02 per update).

---

## 4. RL Agent (`ai/rl_agent.py`)

**Algorithm:** Tabular Q-learning (no neural net).

**What it controls:**
1. `confidence_threshold` — signals below this are suppressed (Gate 2 above)
2. `risk_factor` — multiplier on the base risk% from `config/risk.json`

**State space (162 states):**

State = `{win_rate_bucket}_{conf_bucket}_{session_bucket}_{drawdown_bucket}_{vol_bucket}`

| Dimension | Buckets | Values |
|---|---|---|
| Win rate | 3 | `low` (<40%), `med` (40–60%), `high` (>60%) |
| Confidence | 3 | `low` (<0.55), `med` (0.55–0.70), `high` (>0.70) |
| Session | 3 | `overlap` (12–18 UTC), `active` (07–22 UTC), `quiet` |
| Drawdown | 3 | `low` (<1.5% daily DD), `med` (1.5–3%), `high` (≥3%) |
| Volatility | 2 | `tight` (SL-dist < 1% of entry), `wide` (≥1%) |

3 × 3 × 3 × 3 × 2 = **162 total states**.

The **volatility bucket** uses SL-distance as a cheap ATR proxy — since every strategy sets SL as a multiple of ATR, `|entry − SL| / entry × 100` directly reflects the volatility regime at trade entry without an extra OHLCV fetch:
- `tight` (≈1% of price) — calm forex pairs, equity indices
- `wide` (≥1% of price) — crypto, gold, oil, or forex during volatile sessions

The agent can learn to apply a higher confidence threshold and lower risk factor for wide-stop (high-volatility) trades, independently of whether those trades were profitable.

The **drawdown bucket** was added to give the RL agent awareness of its current risk exposure. As daily drawdown approaches the 5% circuit-breaker limit, the agent enters different state rows and can learn to become more conservative independently of its win-rate and recent confidence levels.

Drawdown is computed in `signal_bus._poll_outcome` after each closed trade:
```python
drawdown_pct = max(0.0, (day_start_balance - balance) / day_start_balance * 100)
```

Backward compatibility: existing 81-key Q-table entries won't match the new 162-state keys (missing `_{vol_bucket}` suffix) and will be treated as unseen states, explored fresh with epsilon. No data migration is needed.

**Action space (9 joint actions):**
Every combination of: `{decrease, hold, increase}` for `conf_threshold` × `{decrease, hold, increase}` for `risk_factor`.

Steps: `conf ±0.02`, `risk ±0.05`.

**Update rule:** Bellman equation, `ALPHA=0.1`, `GAMMA=0.9`. Reward = `profit_pct` of the closed trade.

**Exploration:** Decays from 15% → 2% over time (`EPSILON_START=0.15`, `EPSILON_MIN=0.02`, `EPSILON_DECAY=0.995`). After ~250 trade updates the agent explores ~5% of the time, then continues decaying toward the 2% floor. This allows aggressive early learning and conservative later exploitation.

**Reward normalisation:** Raw `profit_pct` is clipped to `[-0.10, 0.10]` before the Q-table update. This prevents large single-trade spikes (e.g. gold news candles, overnight gaps) from distorting Q-values and causing overconfident position sizing.

**Persistence:** Q-tables saved to `ai/data/rl_qtable_{scalping|day_trading|swing}_{live|paper}.json` after every update (includes `n_updates` counter so epsilon decay persists across restarts). Live and paper tables are kept separate so paper trading doesn't corrupt live learned thresholds. On first run, old unsuffixed files are automatically migrated.

**Hard limits:**
- Operates only on the last ~50 trades from memory for win-rate calculation (inside `trade_memory.stats()`).
- Does not consider news directly — the `NewsFilter` already blocks trades during blackout windows so the RL never observes a near-news trade; adding it to state would be redundant.
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
- Grid search is CPU-bound and synchronous within its background thread. Maximum **2 concurrent optimizer jobs** enforced (`MAX_CONCURRENT_OPT=2`) to prevent event-loop starvation and MT5 heartbeat timeouts. CPU yields (`time.sleep(0)`) between combo iterations allow the FastAPI event loop to stay responsive.
- Simulates spread/slippage cost. Mode-based defaults: `scalping = 0.15R`, `day_trading = 0.05R`, `swing = 0.02R`. Per-symbol overrides take priority for high-spread assets (crypto, gold, indices) so their wider spreads are correctly penalised regardless of trading mode:
  - Crypto (BTCUSD, ETHUSD, XRPUSD, SOLUSD): **0.25–0.30R**
  - Gold / Silver: **0.10–0.12R**; Oil: **0.12R**
  - US/EU indices (US30Cash, US100Cash, GER40Cash …): **0.08–0.10R**
- Minimum **15 signals** fired per combo before it qualifies (raised from 10 — at 10 signals the ±31% confidence interval made scores unreliable; 15 narrows this to ±26%).
- Same-bar SL/TP resolution: when both stop-loss and take-profit levels are touched within the same bar, SL is counted as hitting first (conservative). This prevents walk-forward win rates from being inflated by unrealistic TP-first assumptions on large news candles.
- Optimized params affect the strategy on the **next** run — no restart needed (re-read from `config/optimized_params.json` each cycle).

---

## 6.5 Cross-Symbol Correlation Guard (`engine/strategy_runner.py`)

**What it does:** Prevents the bot from opening multiple positions that all express the same USD directional bet within the same trading mode. This stops compounding losses when a single macro event (e.g. DXY spike) hits all correlated pairs simultaneously.

**How it works:**
- Every trade has a computed `USD_LONG` or `USD_SHORT` label based on symbol and direction:
  - `EURUSD BUY` = USD_SHORT, `EURUSD SELL` = USD_LONG
  - `USDJPY BUY` = USD_LONG,  `USDJPY SELL` = USD_SHORT
  - Same logic applies for GBPUSD, AUDUSD, NZDUSD, USDCAD, USDCHF, XAUUSD, XAGUSD, etc.
- Before a signal is approved for execution, the runner counts how many open bot positions in the **same trading mode** (scalp/day/swing) already carry the same USD direction
- If the count ≥ `max_correlated_positions` (default: `1`), the signal is blocked with `"Correlation guard blocked"`
- Cross-mode stacking is intentional and unaffected — a scalping EURUSD SELL + a swing EURUSD SELL are treated independently
- Non-USD cross pairs (e.g. EURGBP, EURJPY) are not checked (USD direction is `None` for those)

**Effect:** Maximum 1 open USD_LONG or USD_SHORT position per mode at a time. Reduces drawdown concentration on DXY-driven sessions.

**Config:** `max_correlated_positions` in `config/app.json` (top-level, default `1`). Set to `0` to block all correlated stacking; set higher to allow more simultaneous correlated positions.

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
    ├── LSTM (40%)*: predictor.predict(symbol, df, trading_type) → 0–1 probability
    │                 *skipped (returns 0.5) if price_prediction_enabled=false
    │                 *GPU-accelerated if CUDA available, else CPU
    ├── R:R (25%): reward/risk capped at 4:1
    ├── Trend (20%): EMA50 vs EMA200 alignment
    └── Volume (15%): last bar vs 20-bar avg
    │   (weights are configurable via app.json → ai.scorer_weights)
    │
    ▼
Gate 1 — Static filter (if confidence_filter_enabled=true):
    confidence < threshold → BLOCK signal
    │
    ▼
Gate 2 — RL dynamic gate (if rl_agent_enabled=true):
    confidence < rl_agent.confidence_threshold → BLOCK signal
    │   + RL risk_factor applied to lot sizing
    │
    ▼
Correlation guard — same-mode USD exposure check:
    open correlated positions ≥ max_correlated_positions → BLOCK signal
    │
    ▼
Signal passed to execution pipeline (risk check → order submit)
    │
    ▼
Trade closes → TradeMemory.record() → RL.observe() (reward clipped ±0.10) → LSTM retrain check
    (triggers: every 20 trades, OR model age > 7 days with ≥10 trades, OR 5 consecutive losses)
```

---

## 8. Configuration Reference

All AI flags live in `config/app.json` under the `"ai"` key:

| Key | Default | Effect |
|---|---|---|
| `price_prediction_enabled` | `true` | When `true`: LSTM runs and contributes 40% to the signal score. When `false`: LSTM sub-score returns `0.5` (neutral) — signal still scored via R:R, trend, and volume only |
| `confidence_filter_enabled` | `false` | When `true`: enables Gate 1 (static threshold block) |
| `confidence_threshold` | `60` | Gate 1 threshold in percent (60 = block below 0.60) |
| `rl_agent_enabled` | `true` | When `true`: RL gate filters signals + risk_factor scales lot size. When `false`: gate bypassed, lot sizing uses raw risk.json |
| `scorer_weights.lstm` | `0.40` | LSTM component weight (auto-normalised to sum=1.0) |
| `scorer_weights.rr` | `0.25` | R:R component weight |
| `scorer_weights.trend` | `0.20` | Trend alignment weight |
| `scorer_weights.volume` | `0.15` | Volume confirmation weight |
| `max_correlated_positions` | `1` | Max open bot positions per mode sharing the same USD direction. Set to `0` to disable all correlated stacking; higher allows more. (Top-level key in `app.json`, not under `ai`) |

**Settings page controls:** The AI/ML Brain page lets you manually trigger retraining and optimization. The circuit breaker (separate from AI) lives in Settings → Risk.

---

## 9. Current Constraints Summary

| Constraint | Value |
|---|---|
| LSTM min bars to train | 70 |
| LSTM sequence length | 60 bars |
| LSTM input features | 7 (close_return, hl_range, oc_body, volume_norm, upper_wick, is_near_news, atr_norm) |
| LSTM training epochs | 20 |
| LSTM GPU | Auto CUDA if available; fallback CPU |
| RL confidence range | 0.40 – 0.85 |
| RL risk factor range | 0.50 – 1.50 |
| RL exploration rate | 15% → 2% (exponential decay over ~250 updates) |
| RL reward clipping | ±0.10 per trade (prevents outlier distortion) |
| Optimizer max grid combos | 64 |
| Optimizer min signals per combo | 15 |
| Optimizer cooldown | 24 hours |
| Optimizer max concurrency | 2 simultaneous jobs |
| Optimizer spread cost — scalping | 0.15R per trade |
| Optimizer spread cost — day trading | 0.05R per trade |
| Optimizer spread cost — swing | 0.02R per trade |
| Optimizer spread cost — crypto | 0.25–0.30R per trade (symbol override) |
| Optimizer spread cost — gold/silver | 0.10–0.12R per trade (symbol override) |
| Optimizer spread cost — indices | 0.08–0.10R per trade (symbol override) |
| RL state space | 162 states (WR × conf × session × drawdown × volatility) |
| RL drawdown buckets | low (<1.5%), med (1.5–3%), high (≥3% daily DD) |
| RL volatility buckets | tight (SL-dist <1%), wide (≥1%) — ATR proxy |
| LSTM accuracy gate | Skip save if val accuracy < 52% |
| LSTM news flag window | Per-bar tail window (45 min ÷ TF minutes) |
| Backtester SL/TP same-bar | SL counted first (conservative) |
| US market holiday blocking | NYSE/NASDAQ holidays enforced for stock/us_index |
| Paper trade close price | Last MT5-synced `price_current` (not fresh bid/ask) |
| Auto-retrain trigger 1 | Every 20 closed trades per symbol×mode |
| Auto-retrain trigger 2 | Model age > 7 days (requires ≥ 10 trades) |
| Auto-retrain trigger 3 | 5 consecutive losses for symbol×mode |
| Auto-optimizer trigger | Win rate < 45% with ≥ 20 trades |
| Correlation guard | Max 1 same-direction USD position per mode (configurable) |
| Trade memory RAM buffer | Last 5,000 entries |
