# EVOTRADE-AI — AI / ML System: Full Capabilities

**Last Updated:** April 2026

---

## Component Overview

| Component | Purpose | Runs when | Updates when |
| --- | --- | --- | --- |
| **LSTM Predictor** | Directional probability (0–1) per symbol×mode | Every signal check | Every 20th live trade, model stale >7 days, 8 consecutive losses, or manual retrain |
| **Signal Scorer** | Blend LSTM + R:R + Trend + Volume → confidence | Every signal check | Weights hot-reload from `app.json` every 5 s |
| **Regime Classifier** | 6-label market condition label | Every signal check | Per bar — 3-bar hysteresis before label promoted |
| **RL Agent** | Learns confidence threshold + risk factor per mode | Every signal check (gate) | After every closed trade |
| **Param Optimizer** | Best strategy params via walk-forward grid search | On-demand or auto on WR drop | After each optimizer run |
| **Trade Memory** | Central outcome log — source of truth for all AI | Always (every closed trade) | Appended after each close |

---

## 1. LSTM Price Predictor (`ai/predictor.py`)

**What it predicts:** Whether the cumulative return over the next N bars will be positive or negative. Returns a probability between 0 and 1 — `0.5 = neutral`, `>0.65 = strong directional signal`.

**Multi-bar lookahead label** (not single next-bar):

- Scalping (M5): 5 bars ahead = 25-minute horizon
- Day Trading (H1): 5 bars ahead = 5-hour horizon
- Swing (H4): 3 bars ahead = 12-hour horizon

Single next-bar direction at M5/H1/H4 is near-pure noise. Aggregating N bars reduces label noise while keeping the prediction horizon relevant to the trading type.

**Architecture:**

- 2-layer LSTM, hidden size 64, dropout 0.2
- Sequence length: 60 bars (sees 60 bars before making a prediction)
- Input features per bar — **7 total**:

| Feature | Formula | Purpose |
| --- | --- | --- |
| `close_return` | (close[i] − close[i−1]) / close[i−1] | Price momentum |
| `hl_range` | (high − low) / close | Bar range / volatility |
| `oc_body` | (close − open) / open | Candle direction + strength |
| `volume_norm` | volume / mean volume | Relative volume |
| `upper_wick` | (high − close) / close | Rejection signal |
| `is_near_news` | 1.0 for last N bars within 45-min news window, else 0.0 | News event awareness |
| `atr_norm` | ATR(14) / close | Volatility regime |

- Output: binary classification (up vs down)
- Loss: `BCEWithLogitsLoss` with `pos_weight` for class balancing (prevents majority-class dominance)
- Training: 50 epochs, mini-batch shuffled (shuffles sequence order, not internal bar order), label smoothing 0.05, gradient clipping `max_norm=1.0`
- Train/val split: 80/20 on the feature array with scaler fitted on training rows only (no val leakage)

**Accuracy gate:**

- If a model already exists on disk: new model must exceed **58%** validation accuracy or it is discarded (existing model retained)
- First-time training (bootstrap): accepts any accuracy > 50% and logs a bootstrap notice
- 58% threshold: chosen because below this, expected value after spread and slippage is negative

**Model versioning:**

- **Live model:** `ai/models/{key}_lstm.pt` + `{key}_scaler.pkl` + `{key}_meta.json`
- **Anchor model:** `ai/models/{key}_anchor.pt` — frozen at first production-quality training, never auto-overwritten. Used for baseline comparison alerts.
- **Version archive:** `ai/models/versions/{key}/` — last 3 versions kept. API rollback endpoint: `POST /ai/models/rollback/{symbol}/{trading_type}`

**Platt scaling calibration:**
After 30+ live trades per symbol×mode, `POST /ai/models/calibrate/{symbol}/{trading_type}` fits a logistic regression (Platt scaling) on raw sigmoid outputs vs actual outcomes. The calibration parameters (a, b) are persisted to `ai/models/lstm_calibration.json` and applied on every `predict()` call. This maps raw sigmoid confidence to true P(win | confidence).

**One model per symbol × trading type.** Training timeframes:

| Mode | Training TF | Lookahead |
| --- | --- | --- |
| Scalping | M5 | 5 bars (25 min) |
| Day Trading | H1 | 5 bars (5 hours) |
| Swing | H4 | 3 bars (12 hours) |

**Auto-retrain triggers (whichever fires first):**

1. Every 20th closed live/paper trade per symbol×mode
2. Model age > 7 days AND ≥ 10 trades exist for the symbol×mode
3. 8 consecutive losses for the symbol×mode (regime-change indicator)
4. Manual: "Retrain" or "Retrain All" on the AI/ML Brain page

**Anchor vs live accuracy comparison** (feed alert at −8+ point drop):
The performance monitor checks every 5 minutes and broadcasts a WebSocket alert if the live model's training accuracy has dropped more than 8 percentage points below the anchor baseline.

---

## 2. Signal Scorer (`ai/signal_scorer.py`)

**What it produces:** A single 0–1 confidence score per signal from a weighted blend of four components.

### Default Weight Profiles

**Global default (day_trading):**

| Component | Weight | Source |
| --- | --- | --- |
| LSTM direction probability | 30% | `predictor.predict()` |
| Risk:Reward ratio quality | 35% | From entry / SL / TP |
| Trend alignment (EMA50 vs EMA200) | 20% | Computed from OHLCV |
| Volume confirmation | 15% | Last bar vs 20-bar avg |

**Scalping-specific weights** (`app.json → ai.scalping_scorer_weights`):

| Component | Weight | Rationale |
| --- | --- | --- |
| LSTM | 15% | M5 LSTM less reliable than H1/H4 |
| R:R | 25% | Structure quality gate |
| Trend | 40% | Primary scalping edge |
| Volume | 20% | Momentum confirmation |

**Swing-specific weights** (`app.json → ai.swing_scorer_weights`):

| Component | Weight | Rationale |
| --- | --- | --- |
| LSTM | 40% | H4 LSTM is most reliable |
| R:R | 30% | Multi-day commitment requires strong setup |
| Trend | 25% | Weekly trend alignment |
| Volume | 5% | Less meaningful on H4 |

### Regime-Adaptive Weights

When the regime classifier provides a label, the scorer uses a per-regime profile from `app.json → ai.regime_weights`. Resolution priority: per-regime → global default → class defaults.

| Regime | LSTM | RR | Trend | Volume | Logic |
| --- | --- | --- | --- | --- | --- |
| `trending_bull` | 0.35 | 0.25 | **0.30** | 0.10 | Lean on trend + LSTM for timing |
| `trending_bear` | 0.35 | 0.25 | **0.30** | 0.10 | Mirror of trending_bull |
| `ranging_low_vol` | 0.25 | **0.40** | 0.10 | 0.25 | RR structure + volume carry weight |
| `ranging_high_vol` | 0.20 | **0.45** | 0.05 | 0.30 | Trend near-zeroed in noisy range |
| `volatile_breakout` | 0.25 | **0.40** | 0.20 | 0.15 | RR gates explosive moves |
| `quiet` | 0.35 | 0.30 | 0.20 | 0.15 | Near-default; no dominant edge |

### Component Details

**Trend score — graduated (not binary):**

- `gap_pct = (EMA50 − EMA200) / |EMA200|`
- `trend_strength = clip(gap_pct / 0.05, −1, +1)` — normalised: ±5% gap = ±1
- `raw_score = 0.5 + 0.4 × trend_strength` — range **[0.1, 0.9]**
- BUY uses `raw_score`; SELL uses `1 − raw_score`

**R:R score:**

- `clip((reward / risk) / 4.0, 0.0, 1.0)`
- 1:1 → 0.25 | 2:1 → 0.50 | 3:1 → 0.75 | ≥4:1 → 1.0

**Volume score:**

- `clip(last_bar_volume / (20_bar_avg × 2), 0.0, 1.0)`
- At average → 0.50 | 2× above average → 1.0

**Multi-timeframe penalty:**
After scoring, if a higher-TF dataframe is available, the score is penalised when the HTF trend conflicts with the signal direction:

- Aligned (HTF trend score ≥ 0.55): no penalty
- Neutral (0.45–0.55): score × 0.95
- Conflicted (< 0.45): score × 0.85

**Score thresholds (for badge colours in UI):**

- `≥ 0.75` → High confidence (green)
- `0.50–0.74` → Medium (yellow)
- `< 0.50` → Low (gray)

**Graceful degradation:** If LSTM is untrained or unavailable, the LSTM sub-score returns `0.5` (neutral). The overall score still reflects R:R, trend, and volume.

---

## 3. Confidence Gates

Signals pass through **two independent gates** before reaching the execution pipeline.

### Gate 1 — Static Threshold (configurable, off by default)

Controlled by `config/app.json → ai.confidence_filter_enabled` and `ai.confidence_threshold`.

- `false` (default): no static filtering — all scored signals proceed
- `true`: signals below `confidence_threshold / 100` are blocked and logged

### Gate 2 — RL Dynamic Gate

Controlled by `config/app.json → ai.rl_agent_enabled`.

- `true` (default): RL agent's learned `conf_thresh` filters signals; `risk_factor` scales lot size
- `false`: gate bypassed — all signals pass, raw lot sizing from `risk.json`

**Both gates and the signal_bus auto-execution check now use the RL agent threshold as the single source of truth.** The previous triple-gate inconsistency (static threshold, RL threshold, and hardcoded minimums) has been removed — only the RL gate determines tradeability in `signal_bus.py` auto-execution.

---

## 4. RL Agent (`ai/rl_agent.py`)

**Algorithm:** Tabular Q-learning — fast, interpretable, no GPU required.

**What it controls:**

1. `confidence_threshold` — minimum signal confidence to allow entry
2. `risk_factor` — multiplier on the base risk% from `risk.json`

**State space (162 states):**
`{win_rate_bucket}_{conf_bucket}_{session_bucket}_{drawdown_bucket}_{vol_bucket}`

| Dimension | Buckets | Values |
| --- | --- | --- |
| Win rate | 3 | low (<40%), med (40–60%), high (>60%) |
| Avg confidence | 3 | low (<0.55), med (0.55–0.70), high (>0.70) |
| Session | 3 | overlap (12–17 UTC), active (07–22 UTC), quiet |
| Daily drawdown | 3 | low (<1.5%), med (1.5–3%), high (≥3%) |
| Volatility | 2 | tight (SL-dist <1% of entry), wide (≥1%) |

**Actions:** 9 joint actions combining ±CONF_STEP (0.02) and ±RISK_STEP (0.05) or hold.

**Per-mode learning rates:**

- Scalping: α = 0.15 (fast intraday adaptation)
- Day Trading: α = 0.10 (balanced)
- Swing: α = 0.05 (slow — multi-day confirmation needed)

**Per-mode parameter bounds:**

| Mode | conf_thresh range | conf_ceil | risk_factor range |
| --- | --- | --- | --- |
| Scalping | 0.52–0.72 | 0.72 | 0.60–1.50 |
| Day Trading | 0.52–0.78 | 0.78 | 0.60–1.50 |
| Swing | 0.50–0.78 | 0.78 | 0.60–1.50 |

**Reward shaping:**

- Asymmetric clipping: max(−0.05, min(+0.15, profit_pct/100))
- Win-rate penalty: −0.02 reward when win_rate < 40%; −0.01 when < 45%
- Prevents gambler's-fallacy learning ("take everything to recover losses")

**Exploration:** ε-greedy with exponential decay from 15% → 2% over ~250 trades. Tie-breaking defaults to HOLD action on untrained states.

**Isolation:** Paper and live each have their own Q-table files (`rl_qtable_{type}_{mode}.json`). On first live run, paper Q-table is used to bootstrap live (identical market data, same patterns apply).

**Idle decay:** If no trade has closed for 6+ hours (scalping), 6h (day trading), or 12h (swing) and `conf_thresh` is above the default 0.55, it decays by one step. A dedicated background task (`_rl_idle_decay_loop`) runs every 30 minutes to cover weekends and holidays when `observe()` never fires.

**Persistence:** Q-table saved every 10 updates (not every trade) to reduce I/O. Force-saved on graceful shutdown.

**Bootstrap sequence:** Run `python ai/run_rl_bootstrap.py` after completing retrain AND optimizer. This seeds Q-tables using backtests scored with real LSTM confidence. Requires both to complete first — without LSTM models, all backtest conf_scores are 0.55 uniform (no differentiation).

---

## 5. Market Regime Classifier (`engine/regime_classifier.py`)

**Labels:** 6 mutually exclusive market condition labels

| Label | Condition |
| --- | --- |
| `trending_bull` | ADX ≥ threshold, EMA50 > EMA200 |
| `trending_bear` | ADX ≥ threshold, EMA50 < EMA200 |
| `ranging_low_vol` | ADX < threshold, ATR% < 0.80% |
| `ranging_high_vol` | ADX < threshold, ATR% ≥ 0.80% |
| `volatile_breakout` | ADX ≥ breakout threshold AND ATR% ≥ 1.20% AND ADX rising ≥2 units in 3 bars |
| `quiet` | ATR% < 0.10% |

**Per-asset-class ADX thresholds:**

| Asset Class | Trending | Breakout |
| --- | --- | --- |
| Forex | 22 | 30 |
| Commodities (Gold, Oil) | 25 | 32 |
| Indices (US30, GER40) | 28 | 35 |
| Crypto | 30 | 40 |

**Hysteresis:** Raw label must persist for 3 consecutive bars before being promoted to confirmed. Exception: quiet → non-quiet transition promotes immediately (no stuck-in-quiet deadlock).

**State persistence:** Confirmed labels and pending counters are saved to `data/regime_state.json` and restored on restart. A cold-start doesn't reset weeks of accumulated regime context.

**Wiring into the pipeline:**

1. Regime classified from `primary_df` early in `strategy_runner._run_strategy()`
2. Regime label passed to `optimizer.get_params(strat, symbol, regime=regime)` for regime-specific params
3. Regime label passed to `scorer.score(..., regime=regime)` for regime-specific weights
4. Regime label stored in signal dict → `TradeOutcome.regime` → `trade_memory.stats_by_regime()`

**Regime gating (strategy whitelist):** Strategies structurally mismatched with the current regime are blocked before any signal logic runs:

| Regime | Allowed strategies |
| --- | --- |
| `trending_bull/bear` | macd_ema_trend, ema_trend_rider, sr_breakout, ema_scalp, bb_squeeze |
| `ranging_low/high_vol` | vwap_reversion, bb_squeeze, fibonacci_rsi, rsi_divergence |
| `volatile_breakout` | sr_breakout, weekly_breakout, bb_squeeze |
| `quiet` | None (no trades in stationary markets) |

**Regime-aware lot sizing:** In addition to regime gating, approved trades in suboptimal regimes have their lot size reduced:

- `volatile_breakout`: ×0.75
- `ranging_high_vol`: ×0.85
- `ranging_low_vol`: ×0.90
- `trending_*`: ×1.00 (no reduction)

---

## 6. Parameter Optimizer (`ai/param_optimizer.py`)

**Method:** Walk-forward grid search with train/val split. Best combo by composite score saved per strategy×symbol.

**Train/val split:** 75% training, 25% validation. Extra secondary-TF dataframes are also split at the same boundary to prevent lookahead bias. Score = 40% train + 60% validation (penalises overfitting).

**Spread cost deduction (per trade, from R-multiple):**

| Instrument | Cost |
| --- | --- |
| Scalping (generic) | 0.20R |
| Day Trading | 0.08R |
| Swing | 0.03R |
| Crypto (BTCUSD, ETHUSD, etc.) | 0.40R |
| Gold / Silver | 0.15–0.18R |
| Oil / Energy | 0.15–0.20R |
| US/EU Indices | 0.10–0.12R |
| Stocks | 0.30–0.35R |

**Per-regime best params:** During the grid search, the best param combo for each observed regime label is tracked separately. These are stored in `config/optimized_params.json` under `by_regime` and used when the regime classifier provides a matching label at signal time.

**Persistence:** Atomic writes (`mkstemp → os.replace`) — a crash or kill during optimisation cannot corrupt `optimized_params.json`. Version archive keeps last 5 copies in `config/params_archive/`.

**Corruption recovery:** If `optimized_params.json` is empty or unparseable (e.g. from a pre-fix non-atomic write), `_load_opt()` automatically attempts to restore the most recent valid archive copy. If no archive exists, the file is deleted and the next save starts clean.

**Max grid combos:** 64 per strategy (randomly sampled if the full grid exceeds this).

**Min signals per combo:** 10 — combos that fired fewer backtest signals are discarded.

**Cooldown:** 24 hours between automatic re-optimisations of the same strategy×symbol.

**Concurrency:** Maximum 2 simultaneous optimizer jobs (prevents CPU starvation and MT5 lock contention). Additional jobs queue automatically.

**Auto-trigger:** When `trade_memory.should_reoptimize()` returns True — win rate dropped below 45% with ≥ 30 new live trades since the last optimisation.

**Standalone script:** `python ai/run_optimizer.py` — 12 parallel OS processes (true CPU parallelism), skips already-done jobs (bars_used ≥ 30,000), safe to stop and resume.

**Execution order dependency:** Optimizer is independent of LSTM models and can run in parallel with `run_retrain.py`. Bootstrap (`run_rl_bootstrap.py`) should wait for both to complete.

---

## 7. Trade Memory (`ai/trade_memory.py`)

**Central data store** for all closed trade outcomes.

**Sources:**

| Source | When written | Mode tag |
| --- | --- | --- |
| Live trades | After MT5 position closes via `_poll_outcome` | `"live"` |
| Paper trades | After MT5 position closes or paper ledger sync | `"paper"` |
| Backtest simulations | After each backtest run | `"backtest"` |

**Storage:**

- Disk: `ai/data/trade_memory.jsonl` — append-only, unlimited size
- RAM: rolling buffer of last **10,000** entries for fast reads

**Key fields per entry:**

| Field | Description |
| --- | --- |
| `ticket` | MT5 ticket (0 for backtest entries) |
| `symbol`, `strategy`, `trading_type`, `direction` | Trade identification |
| `confidence` | Signal scorer score at entry time |
| `lstm_predicted_direction` | "BUY" or "SELL" — LSTM prediction at signal time |
| `regime` | Market regime label at signal time |
| `outcome` | `tp_hit`, `sl_hit`, `manual_close`, `timeout` |
| `profit`, `profit_pips`, `profit_pct` | P&L in three formats |
| `duration_mins` | Hold time in minutes |
| `mode` | `live`, `paper`, or `backtest` |
| `extra.source` | Detailed source tag |
| `extra.slippage_pips` | Execution slippage in pips |

**Analytics methods:**

| Method | Description |
| --- | --- |
| `stats()` | Win rate, avg P&L, TP/SL counts, slippage tracking |
| `lstm_accuracy()` | Live LSTM prediction accuracy vs actual outcomes; flags degraded symbols |
| `stats_by_regime()` | Win rate and avg P&L per regime per strategy |
| `detect_drift()` | Page-Hinkley test on rolling win rate — detects regime shifts |
| `rolling_ev_stability()` | EV trend across rolling windows (improving/stable/degrading) |

---

## 8. Correlation Guard

**Prevents:** Multiple positions in the same asset group and direction within the same trading mode.

**Asset groups covered:**

| Group | Members (examples) |
| --- | --- |
| USD Short (BUY = USD weakens) | EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD |
| USD Long (BUY = USD strengthens) | USDJPY, USDCAD, USDCHF |
| Crypto Long/Short | BTCUSD, ETHUSD, XRPUSD, SOLUSD |
| Gold / Silver Long/Short | GOLD, SILVER, XAUUSD, XAGUSD |
| Oil / Energy Long/Short | USOIL, BRENTCash, NGASCash |
| US Indices Long/Short | US30Cash, US100Cash, US500Cash |
| EU / Asia Indices Long/Short | GER40Cash, UK100Cash, FRA40Cash |
| Tech Stocks Long/Short | Tesla, Nvidia, Apple, Microsoft, Amazon, Meta |

Direction is included in the group key — a long BTC and a short ETH are **not** blocked. Cross-mode stacking is allowed (a scalping BUY and a swing BUY on the same symbol are independent). `max_correlated_positions` in `app.json` (default 1) sets the per-group-per-mode limit.

---

## 9. Full Signal Lifecycle

```
MT5 bar arrives (bar-close guard verified)
    │
    ▼
Bar-close guard: new primary-TF bar closed? (2-bar OHLCV check)
    No → skip this tick
    │
    ▼
Strategy generates raw signal (entry, SL, TP) using optimized params
    │
    ▼
Regime classifier labels market condition from primary_df (3-bar hysteresis)
    │
    ▼
Regime gate: is this strategy allowed in the current regime?
    No → signal suppressed
    │
    ▼
SignalScorer.score() → 0–1 confidence
    ├── LSTM (weight varies by mode/regime): predictor.predict() → 0–1
    ├── R:R quality: (reward/risk)/4.0 capped at 1.0
    ├── Trend (EMA50 vs EMA200): graduated score [0.1–0.9]
    └── Volume: last bar vs 20-bar avg
    + MTF penalty if higher-TF trend conflicts
    │
    ▼
Gate 1 — Static threshold (if confidence_filter_enabled):
    confidence < threshold → BLOCK
    │
    ▼
Gate 2 — RL dynamic gate (if rl_agent_enabled):
    confidence < rl_agent.conf_thresh → BLOCK
    + RL risk_factor × base risk%
    │
    ▼
Correlation guard: open correlated positions ≥ max_correlated_positions → BLOCK
    │
    ▼
Regime lot reduction: ×0.75–1.00 based on regime
Volatility lot reduction: ATR% vs baseline → up to ×0.50
    │
    ▼
Signal dispatched to execution pipeline (news/session/spread check → order)
    │
    ▼
Trade opens → _poll_outcome() monitors every 30 s
    ├── Trailing stop (ATR-based on H1 or H4)
    ├── Partial close at TP1 + move SL to breakeven (day trading)
    └── Swing breakeven at 50% TP distance + ATR trail
    │
    ▼
Trade closes → TradeMemory.record() (with regime, slippage, mode, lstm_direction)
    │
    ▼
RL.observe(win_rate, avg_conf, reward, drawdown_pct, vol_pct)
    │
    ▼
Auto-retrain check (every 20 trades / stale >7 days / 8 consecutive losses)
Auto-optimizer check (win rate < 45% with ≥ 30 new trades)
```

---

## 10. Configuration Reference

All AI flags in `config/app.json` under `"ai"`:

| Key | Default | Effect |
| --- | --- | --- |
| `price_prediction_enabled` | `true` | LSTM contributes to signal score; `false` → LSTM returns 0.5 neutral |
| `confidence_filter_enabled` | `false` | Gate 1 static threshold enabled/disabled |
| `confidence_threshold` | `60` | Gate 1 threshold in percent (60 = block below 0.60) |
| `rl_agent_enabled` | `true` | Gate 2 RL filtering + risk_factor lot scaling |
| `scorer_weights` | `{lstm:0.30, rr:0.35, trend:0.20, volume:0.15}` | Global weights (day trading default) |
| `scalping_scorer_weights` | `{lstm:0.15, rr:0.25, trend:0.40, volume:0.20}` | Scalping-specific weights |
| `swing_scorer_weights` | `{lstm:0.40, rr:0.30, trend:0.25, volume:0.05}` | Swing-specific weights |
| `regime_weights` | 6 regime profiles | Per-regime weight overrides |
| `max_correlated_positions` | `1` | Max open positions per asset group per mode |

---

## 11. Constraints & Limits

| Parameter | Value |
| --- | --- |
| LSTM sequence length | 60 bars |
| LSTM input features | 7 |
| LSTM training epochs | 50 |
| LSTM multi-bar lookahead | 5 bars (scalping/day), 3 bars (swing) |
| LSTM accuracy gate (existing model) | 58% |
| LSTM accuracy gate (first bootstrap) | 50% |
| LSTM GPU | Auto CUDA; fallback CPU |
| LSTM model versions kept | 3 (+ anchor) |
| RL confidence range | 0.40–0.85 (mode-specific floor/ceil) |
| RL risk factor range | 0.60–1.50 |
| RL state space | 162 states |
| RL exploration decay | 15% → 2% (ε-greedy, ~250 trades) |
| RL reward clipping | max(−0.05, min(+0.15, reward)) |
| RL save frequency | Every 10 updates |
| Optimizer max grid combos | 64 |
| Optimizer min signals per combo | 10 |
| Optimizer train/val split | 75% / 25% |
| Optimizer validation blend | 40% train + 60% val score |
| Optimizer cooldown | 24 hours |
| Optimizer max concurrency | 2 jobs |
| Trade memory RAM buffer | 10,000 entries |
| Auto-retrain trigger 1 | Every 20 closed live/paper trades per symbol×mode |
| Auto-retrain trigger 2 | Model age > 7 days (requires ≥ 10 trades) |
| Auto-retrain trigger 3 | 8 consecutive losses |
| Auto-optimizer trigger | Win rate < 45% + ≥ 30 new trades |
| News filter coverage | This week + next week (date-param attempt) |
| News filter TTL | 60 minutes (configurable) |
