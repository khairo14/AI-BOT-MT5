# Regime Classification & Analytics — Feature Reference

**Introduced:** March 2026  
**Motivation:** Live trading data review (29.2% win rate on 24 trades; SOL+XRP+ETH triple-loss same session) identified two root causes — signals were not weighted by market condition, and correlated positions across all asset classes were not blocked.

---

## Overview of Changes

| # | Area | Files Changed |
|---|---|---|
| 1 | Signal scorer weights — Balanced profile | `config/app.json` |
| 2 | Regime classifier | `engine/regime_classifier.py` *(new)* |
| 3 | Regime wired into scoring pipeline | `ai/signal_scorer.py`, `engine/strategy_runner.py` |
| 4 | Correlation guard extended to all asset classes | `engine/strategy_runner.py` |
| 5 | Regime-aware parameter optimizer | `ai/param_optimizer.py` |
| 6 | Analytics API | `api/routes/analytics.py` *(new)*, `api/main.py` |
| 7 | Analytics dashboard page | `dashboard/app/analytics/page.tsx` *(new)*, `dashboard/components/nav/Sidebar.tsx`, `dashboard/types/index.ts`, `dashboard/lib/api.ts` |

---

## 1. Signal Scorer Weights — Balanced Profile

**File:** `config/app.json` → `ai.scorer_weights`

The default weight vector was updated from the previous **Prediction-Driven (Aggressive)** profile to **Balanced**:

| Component | Old | New | Rationale |
|---|---|---|---|
| LSTM | 0.40 | 0.30 | Reduces dependency on a single model prediction |
| RR | 0.25 | **0.35** | Elevating RR acts as a structural quality gate — only setups with favourable risk:reward pass |
| Trend | 0.20 | 0.20 | Unchanged |
| Volume | 0.15 | 0.15 | Unchanged |

### Regime Weight Profiles

Six per-regime weight overrides are stored in `config/app.json` under `ai.regime_weights`. When the regime classifier identifies the current market condition, the scorer uses the matching profile instead of the global defaults.

| Regime | LSTM | RR | Trend | Volume | Logic |
|---|---|---|---|---|---|
| `trending_bull` | 0.35 | 0.25 | **0.30** | 0.10 | Direction confirmed — lean on trend, LSTM for timing |
| `trending_bear` | 0.35 | 0.25 | **0.30** | 0.10 | Same as bull, mirrored direction |
| `ranging_low_vol` | 0.25 | **0.40** | 0.10 | 0.25 | No trending edge — RR structure + volume confirmation carry the weight |
| `ranging_high_vol` | 0.20 | **0.45** | 0.05 | 0.30 | Noisy range — RR and volume dominance; trend component nearly zeroed |
| `volatile_breakout` | 0.25 | **0.40** | 0.20 | 0.15 | Breakout conditions — RR gates explosive moves, trend confirms direction |
| `quiet` | 0.35 | 0.30 | 0.20 | 0.15 | Near-default; quiet periods have no dominant edge signal |

Regime weights are resolved at score time — changing `app.json` takes effect within 5 seconds (hot-reload TTL). No restart required.

---

## 2. Market Regime Classifier

**File:** `engine/regime_classifier.py`

### Problem Addressed

Regime-sensitive logic previously existed in four isolated locations with no shared label:
- `ai/signal_scorer.py` — EMA trend score (20% weight component)
- `ai/predictor.py` — ATR normalised feature (LSTM input only)
- `ai/rl_agent.py` — volatility/session Q-table buckets
- Per-strategy internal ADX filter thresholds

Each component had its own interpretation of "trending" vs "ranging". The new classifier provides a single authoritative label used by all downstream components.

### Regime Labels

| Label | Condition |
|---|---|
| `trending_bull` | ADX ≥ threshold, EMA50 > EMA200 |
| `trending_bear` | ADX ≥ threshold, EMA50 < EMA200 |
| `ranging_low_vol` | ADX < threshold, ATR% < 0.80% |
| `ranging_high_vol` | ADX < threshold, ATR% ≥ 0.80% |
| `volatile_breakout` | ADX ≥ breakout threshold, ATR% ≥ 1.20%, ADX rising ≥2 units in 3 bars |
| `quiet` | ATR% < 0.10% (market virtually stationary) |

### Per-Asset-Class ADX Thresholds

Crypto and indices structurally produce higher ADX readings than forex pairs at the same "trendiness" level. Using a single forex-calibrated threshold would classify most crypto moves as persistent trending:

| Asset Class | Examples | ADX Trending | ADX Breakout |
|---|---|---|---|
| Crypto | BTCUSD, ETHUSD, XRPUSD, SOLUSD | 30 | 40 |
| Indices | US30, US100, GER40, JPN225 | 28 | 35 |
| Commodities | XAUUSD, USOIL, BRENT, NGAS | 25 | 32 |
| Forex | EURUSD, GBPUSD, USDJPY, etc. | 22 | 30 |

### Hysteresis

A raw classification must hold for **3 consecutive bars** before it replaces the confirmed label. This prevents the weight vector from oscillating when the market sits on a regime boundary (e.g. ADX hovering near 22 on EURUSD).

```
bar N:   raw = "trending_bull"   pending=1/3  confirmed="ranging_low_vol"
bar N+1: raw = "trending_bull"   pending=2/3  confirmed="ranging_low_vol"
bar N+2: raw = "trending_bull"   pending=3/3  confirmed → "trending_bull"  ✓
```

If the raw label changes before reaching 3, the counter resets to 1 for the new label.

### Usage

```python
from engine.regime_classifier import regime_classifier

label = regime_classifier.classify("BTCUSD", df)   # → "volatile_breakout"
label = regime_classifier.current_label("EURUSD")  # → last confirmed, no recompute
all   = regime_classifier.all_labels()             # → {"EURUSD": "ranging_low_vol", ...}
```

`classify()` is thread-safe. `regime_classifier` is a module-level singleton — import from anywhere, same instance.

Minimum DataFrame requirement: **210 rows** of OHLCV (to compute EMA200). Columns required: `open`, `high`, `low`, `close`, `volume`. All strategy primary-TF bar counts are set to **250** in `TIMEFRAME_BARS` to ensure this minimum is always met (see §3).

---

## 3. Scoring Pipeline — Regime Integration

**Files:** `ai/signal_scorer.py`, `engine/strategy_runner.py`

### Execution Order (after audit fix)

`strategy_runner._run_strategy()` was restructured so regime is classified **before** strategy parameters are selected. The previous order caused regime-aware optimizer params to never be used:

```
❌ Old order:
  1. _strategy_params(strat_name, symbol)   ← regime unknown here
  2. fetch tf_data
  3. classify regime
  4. scorer.score(…, regime=regime)

✅ New order:
  1. fetch tf_data                          ← df available for classifier
  2. classify regime from primary_df
  3. _strategy_params(strat_name, symbol, regime=regime)  ← regime-aware params
  4. strategy instantiated with correct params
  5. scorer.score(…, regime=regime)
```

`_PRIMARY_TF` is now a **module-level constant** (previously it was re-defined inside the scoring try-block on every signal, and used M1 for ema_scalp instead of the more stable M5).

### Bar Count Fix

All strategy primary-TF entries in `TIMEFRAME_BARS` were raised to **250** (previous values ranged from 80 to 220). The regime classifier needs EMA200 = minimum 210 bars. With the old values, most strategies (scalping M5:100, day-trading M15:200, H1:150) would always return `"quiet"` because `_classify_raw` exits early when `len(df) < 210`.

| Strategy | Primary TF | Old bars | New bars |
|---|---|---|---|
| ema_scalp | M5 | 100 | 250 |
| bb_squeeze | M5 | 100 | 250 |
| vwap_reversion | M5 | 200 | 250 |
| macd_ema_trend | H1 | 220 | 250 |
| sr_breakout | H1 | 150 | 250 |
| rsi_divergence | H1 | 100 | 250 |
| fibonacci_rsi | H4 | 100 | 250 |
| weekly_breakout | H4 | 80 | 250 |
| ema_trend_rider | H1 | 250 | 250 (unchanged) |

Note: `ema_scalp` primary TF corrected from M1 to M5 (M1 regime is too noisy and short for EMA200 to be meaningful).

### Weight Resolution

`SignalScorer._weights(regime)` resolves in priority order:
1. `app.json → ai.regime_weights[regime]` — per-regime adaptive weights
2. `app.json → ai.scorer_weights` — global static Balanced weights
3. Class-level defaults — fallback if config file is unreadable

---

## 4. Correlation Guard — All Asset Classes

**File:** `engine/strategy_runner.py`

### Problem Addressed

The original correlation guard only blocked multiple same-direction USD forex positions. Positions in crypto, gold, oil, indices, and stocks were entirely unchecked — directly evidenced by SOL + XRP + ETH all losing the same session (three positions, same direction, fully correlated).

### Coverage

`max_correlated_positions` (configured in `app.json`, default `1`) now applies across all 7 groups:

| Group | Members |
|---|---|
| USD Long (forex) | EURUSD, GBPUSD, AUDUSD, NZDUSD, XAUUSD, XAGUSD, GBPJPY, EURJPY, CADJPY |
| USD Short (forex) | USDCAD, USDCHF, USDJPY, USDMXN, USDZAR, USDSEK, USDDKK, USDNOK |
| Crypto | BTCUSD, ETHUSD, XRPUSD, SOLUSD, ADAUSD, DOTUSD, LTCUSD, BNBUSD |
| Gold / Silver | GOLD, SILVER, XAUUSD, XAGUSD |
| Oil / Energy | USOIL, UKOIL, BRENTCash, WTICash, NGAS |
| US Indices | US30Cash, US100Cash, US500Cash, SPXCash, NDXCash |
| EU / Asia Indices | GER40Cash, UK100Cash, FRA40Cash, JPN225Cash, AUS200Cash |
| Tech Stocks | Tesla, Nvidia, Apple, Microsoft, Amazon, Meta, Alphabet, Netflix, AMD, Intel |

The group key includes direction (`crypto_buy`, `crypto_sell`) so that a long BTC and a short ETH are **not** blocked — they have opposite exposure.

Any symbol not matched to a group bypasses the asset-class check (falls back to USD forex check only), ensuring coverage degrades gracefully for exotic names.

---

## 5. Regime-Aware Parameter Optimizer

**File:** `ai/param_optimizer.py`

### Problem Addressed

Grid-search backtests previously evaluated every parameter combination over the entire backtest window uniformly. A scalping configuration that excels in `volatile_breakout` but degrades in `ranging_low_vol` would be averaged to mediocre and potentially discarded in favour of a mediocre-everywhere alternative.

### Changes

**`_backtest_combo()`** now tracks per-regime stats during the walk-forward window:
- Returns a 4-tuple: `(win_rate, avg_rr, n_trades, regime_stats)`
- `regime_stats` maps each observed regime label → `{win_rate, n_trades}`

**`_run_backtest()`** maintains a `regime_best_params` dict alongside the global best:
- For each combo evaluated, if it scores best for a specific regime, it is stored under that regime key

**`_save_params()`** embeds a `by_regime` sub-dict in `config/optimized_params.json`:
```json
{
  "ema_cross": {
    "EURUSD": {
      "ema_fast": 10, "ema_slow": 50,
      "by_regime": {
        "trending_bull":  { "ema_fast": 8,  "ema_slow": 40 },
        "ranging_low_vol":{ "ema_fast": 14, "ema_slow": 65 }
      }
    }
  }
}
```

**`get_params(strategy, symbol, regime=None)`** resolution order:
1. `by_regime[regime]` — if a regime label is passed and a regime-specific entry exists
2. Global best params — fallback for unknown regimes or first-run (no regime data yet)

The strategy runner passes the current regime label to `get_params()`, so the optimizer output and scoring weights respond to the same regime classification.

---

## 6. Analytics API

**File:** `api/routes/analytics.py` (registered at `/analytics` in `api/main.py`)

### Endpoints

#### `GET /analytics/performance`

Query parameters:

| Parameter | Values | Default |
|---|---|---|
| `account` | `paper`, `live`, `all` | `all` |
| `trading_type` | `scalping`, `day_trading`, `swing`, `all` | `all` |
| `limit` | integer | `5000` |

Reads `data/trade_journal.jsonl`, filters closed trades (`event == "close"`), applies account and trading_type filters, then returns:

| Field | Description |
|---|---|
| `total_trades`, `wins`, `losses`, `win_rate` | Basic counts |
| `total_profit`, `avg_profit`, `avg_win`, `avg_loss` | P&L stats |
| `avg_rr` | Average realised risk:reward |
| `sharpe_ratio` | Annualised Sharpe (daily returns, risk-free = 0) |
| `sortino_ratio` | Annualised Sortino (downside deviation only) |
| `max_drawdown_pct` | Peak-to-trough drawdown on cumulative equity curve (%) |
| `max_losing_streak`, `current_losing_streak` | Consecutive losses |
| `equity_curve` | `[{time, equity}]` — cumulative P&L over time |
| `by_strategy`, `by_symbol`, `by_mode`, `by_account`, `by_hour` | Per-bucket breakdowns |
| `trade_quality` | `{high, medium, low}` — trade count by confidence tier |
| `failure_analysis` | Worst symbols, worst strategies, streak data |

#### `GET /analytics/regime/status`

Returns `{regimes: {symbol: label}}` — a snapshot of all confirmed regime labels currently held by the `regime_classifier` singleton. Only symbols that have been classified at least once appear in the response.

---

## 7. Analytics Dashboard

**File:** `dashboard/app/analytics/page.tsx` (linked from sidebar as `📊 Analytics`)

### Layout

```
┌─ Account Tabs ─────────────────────────────────────┐
│  Paper  |  Live  |  All                            │
├─ Mode Tabs ─────────────────────────────────────────┤
│  All Modes  |  Scalping  |  Day Trading  |  Swing  │
├─────────────────────────────────────────────────────┤
│  [Trades] [Win%] [P&L] [RR] [Sharpe] [Sortino]    │
│  [Max DD]  [Avg Win / Avg Loss]                    │
├─────────────────────────────────────────────────────┤
│  Equity Curve (SVG)                                │
├─────────────────────────────────────────────────────┤
│  Trade Quality Bar  (High | Medium | Low)          │
├─────────────────────────────────────────────────────┤
│  Regime Status Pill Bar  (live, per symbol)        │
├────────────────────┬────────────────────────────────┤
│  By Strategy table │  By Symbol table               │
├────────────────────┴────────────────────────────────┤
│  By Trading Mode table  (only when All Modes)      │
│  By Account table       (only when All accounts)   │
├─────────────────────────────────────────────────────┤
│  Hour-of-Day Heatmap  (24-cell UTC grid)           │
├─────────────────────────────────────────────────────┤
│  ⚠ Failure Analysis                                │
│  Worst symbols | Worst strategies | Streak badges  │
└─────────────────────────────────────────────────────┘
```

### Visual Conventions

- **Equity curve** — filled SVG area with zero baseline; green when final cumulative P&L ≥ 0, red when negative
- **Trade quality bar** — stacked horizontal bar: green = high confidence (score ≥ 0.70), yellow = medium (0.50–0.69), grey = low / no score recorded
- **Regime pills** — color-coded by label: green = bull, red = bear, blue = ranging low-vol, orange = ranging high-vol, purple = volatile breakout, grey = quiet
- **Hour heatmap** — green cell ≥ 60% win rate, yellow ≥ 45%, red < 45%; hover shows trade count
- **Stat card colors** — green if metric is healthy, yellow if borderline, red if poor (thresholds: Sharpe/Sortino ≥ 1.5 green, ≥ 0.5 yellow; drawdown ≤ 8% green, ≤ 15% yellow; win rate ≥ 50% green, ≥ 40% yellow)
- **Current losing streak** — failure panel badge turns red when streak ≥ 3

### Data Flow

```
User changes tab
  → fetchAnalyticsPerformance(account, tradingType)
  → GET /analytics/performance?account=...&trading_type=...
  → api/routes/analytics.py reads data/trade_journal.jsonl
  → returns AnalyticsPerformance JSON
  → page renders all sections

On mount (parallel)
  → fetchRegimeStatus()
  → GET /analytics/regime/status
  → regime_classifier.all_labels()
  → RegimePanel renders pills
```

### TypeScript Types Added

| Type | Location | Purpose |
|---|---|---|
| `EquityPoint` | `dashboard/types/index.ts` | `{time: string, equity: number}` — one point on the equity curve |
| `BucketStats` | `dashboard/types/index.ts` | Shared shape for all breakdown tables (`total`, `wins`, `losses`, `win_rate`, `total_profit`, `avg_profit`) |
| `TradeQuality` | `dashboard/types/index.ts` | `{high, medium, low}` counts |
| `FailureAnalysis` | `dashboard/types/index.ts` | Worst symbols/strategies, streak fields |
| `AnalyticsPerformance` | `dashboard/types/index.ts` | Root response shape from `GET /analytics/performance` |
