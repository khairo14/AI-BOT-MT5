# EVOTRADE-AI — Regime Classification & Analytics

**Last Updated:** April 2026

---

## Overview

The regime classifier and analytics system provide two complementary capabilities:

1. **Regime Classification** — identifies the current market condition for each symbol and wires that label into signal scoring weights, parameter selection, strategy gating, and lot sizing
2. **Analytics** — comprehensive performance statistics from the trade journal, exposed via API and a dedicated dashboard page

---

## 1. Market Regime Classifier (`engine/regime_classifier.py`)

### Labels

| Label | Condition |
|---|---|
| `trending_bull` | ADX ≥ threshold, EMA50 > EMA200 |
| `trending_bear` | ADX ≥ threshold, EMA50 < EMA200 |
| `ranging_low_vol` | ADX < threshold, ATR% < 0.80% |
| `ranging_high_vol` | ADX < threshold, ATR% ≥ 0.80% |
| `volatile_breakout` | ADX ≥ breakout threshold AND ATR% ≥ 1.20% AND ADX rising ≥2 units in 3 bars |
| `quiet` | ATR% < 0.10% (market nearly stationary — no trades) |

### Per-Asset-Class ADX Thresholds

| Asset Class | Examples | ADX Trending | ADX Breakout |
|---|---|---|---|
| Forex | EURUSD, GBPUSD, USDJPY | 22 | 30 |
| Commodities | GOLD, SILVER, USOIL | 25 | 32 |
| Indices | US30Cash, GER40Cash | 28 | 35 |
| Crypto | BTCUSD, ETHUSD | 30 | 40 |

Asset class is determined from `config/symbols.json` via a cache built at startup (refreshed hourly).

### Hysteresis

A raw classification must hold for **3 consecutive bars** before replacing the confirmed label. This prevents oscillation when the market sits near a regime boundary.

```
bar N:   raw = "trending_bull"   pending=1/3  confirmed="ranging_low_vol"
bar N+1: raw = "trending_bull"   pending=2/3  confirmed="ranging_low_vol"
bar N+2: raw = "trending_bull"   pending=3/3  confirmed → "trending_bull"  ✓
```

**Exception:** If the confirmed label is `quiet` and the raw label is any non-quiet label, the promotion is immediate (no 3-bar wait). This prevents the bot from staying stuck in "quiet" mode during a sudden volatility expansion.

### State Persistence

Confirmed labels and pending counters are saved to `data/regime_state.json` after every label change and restored on restart. A server restart does not reset weeks of accumulated regime context.

### Usage

```python
from engine.regime_classifier import regime_classifier

label = regime_classifier.classify("BTCUSD", df)   # → "volatile_breakout"
label = regime_classifier.current_label("EURUSD")  # → last confirmed, no recompute
all   = regime_classifier.all_labels()             # → {"EURUSD": "ranging_low_vol", ...}
```

`classify()` is thread-safe. `regime_classifier` is a module-level singleton.

**Minimum requirement:** 210 rows of OHLCV (for EMA200). All `TIMEFRAME_BARS` entries in `strategy_runner.py` are set to ≥250 to guarantee this.

---

## 2. Regime Integration in the Signal Pipeline

The execution order in `strategy_runner._run_strategy()`:

```
1. Fetch all required TF dataframes (TIMEFRAME_BARS)
2. Classify regime from primary_df  ← regime known here
3. _strategy_params(strat, symbol, regime=regime)  ← regime-aware params
4. Regime gate: is this strategy allowed in current regime?
5. Strategy generates raw signal
6. scorer.score(..., regime=regime)  ← regime-aware weights
7. RL gate, correlation guard, lot reductions
8. Signal dispatched
```

### Regime Gating (Strategy Whitelist)

Strategies structurally mismatched with the current regime are blocked before any computation:

| Regime | Allowed Strategies |
|---|---|
| `trending_bull` / `trending_bear` | macd_ema_trend, ema_trend_rider, sr_breakout, ema_scalp, bb_squeeze |
| `ranging_low_vol` / `ranging_high_vol` | vwap_reversion, bb_squeeze, fibonacci_rsi, rsi_divergence |
| `volatile_breakout` | sr_breakout, weekly_breakout, bb_squeeze |
| `quiet` | None — no trades in stationary markets |

### Regime-Aware Scorer Weights

When a regime label is available, `SignalScorer._weights()` resolves in priority order:
1. `app.json → ai.regime_weights[regime]` — per-regime profile
2. `app.json → ai.scalping_scorer_weights` (if scalping)
3. `app.json → ai.swing_scorer_weights` (if swing)
4. `app.json → ai.scorer_weights` — global balanced profile
5. Class-level defaults

Weight profiles hot-reload from `app.json` every 5 seconds — no restart needed.

### Regime-Aware Parameter Optimizer

During grid-search backtests, the optimizer tracks per-regime win rates for each parameter combo. The best combo for each regime is stored separately in `config/optimized_params.json` under `by_regime`:

```json
{
  "ema_scalp": {
    "EURUSD": {
      "ema_fast": 10, "rr": 1.3,
      "by_regime": {
        "trending_bull":   { "ema_fast": 8,  "rr": 1.5 },
        "ranging_low_vol": { "ema_fast": 13, "rr": 1.2 }
      }
    }
  }
}
```

At signal time, `optimizer.get_params(strat, symbol, regime=regime)` resolves in order: regime-specific → global best → code defaults.

### Regime-Based Lot Sizing

After standard lot calculation, approved signals receive an additional regime multiplier:

| Regime | Lot Factor | Reason |
|---|---|---|
| `trending_bull` / `trending_bear` | ×1.00 | Familiar, well-modelled — full size |
| `ranging_low_vol` | ×0.90 | Slightly reduced — strategies less optimal |
| `ranging_high_vol` | ×0.85 | Wider spreads, slippage risk |
| `volatile_breakout` | ×0.75 | Unpredictable price action — reduce exposure |
| `quiet` | ×0.50 | Shouldn't reach here (regime gate blocks); safety reduction |

---

## 3. Trade Memory Analytics

Beyond basic win/loss stats, `trade_memory.py` provides:

### `detect_drift()` — Page-Hinkley Drift Detection

Monitors whether the rolling win rate is drifting downward (regime shift or model decay). The Page-Hinkley algorithm tracks a cumulative sum and raises an alarm when it exceeds a threshold.

Parameters:
- `window`: baseline window size (default 30 trades)
- `delta`: minimum acceptable mean change (0.005 = 0.5%)
- `lambda_threshold`: sensitivity (default 10.0 — lower = more sensitive)

Returns:
- `drift_detected`: bool
- `drift_direction`: `"down"` (degrading) or `"up"` (improving)
- `baseline_win_rate`, `recent_win_rate`, `win_rate_delta`
- `severity`: `"high"` (>15% WR drop), `"medium"` (>8%), `"low"`, or `"none"`

### `rolling_ev_stability()` — EV Trend Analysis

Tracks expected value across rolling windows (50% overlap) to detect whether performance is improving, stable, or degrading before it reaches the hard 40% alert threshold.

Returns:
- `ev_slope`: linear regression slope over recent windows
- `trend`: `"improving"`, `"stable"`, or `"degrading"`
- `recent_avg_wr`, `recent_avg_ev`

### `lstm_accuracy()` — Live LSTM Accuracy

Compares `lstm_predicted_direction` against actual profitable direction for live trades. Flags symbols where live accuracy is below 45% as `degraded_symbols`.

### `stats_by_regime()` — Regime Performance Breakdown

Win rate and avg P&L per regime per strategy. Validates that regime gating is improving edge (e.g. verifying that trades in `trending_bull` have higher win rate than those in `ranging_high_vol`).

---

## 4. Analytics API

### `GET /analytics/performance`

Query parameters:

| Parameter | Values | Default |
|---|---|---|
| `account` | `paper`, `live`, `all` | `all` |
| `trading_type` | `scalping`, `day_trading`, `swing`, `all` | `all` |
| `limit` | integer (1–50,000) | `5000` |

Returns comprehensive performance data from `data/trade_journal.jsonl`:

| Field | Description |
|---|---|
| `total_trades`, `wins`, `losses`, `win_rate` | Basic counts |
| `total_profit`, `avg_profit`, `avg_win`, `avg_loss` | P&L stats |
| `avg_rr` | Average realised risk:reward |
| `sharpe_ratio` | Annualised Sharpe (daily returns, risk-free = 0) |
| `sortino_ratio` | Annualised Sortino (downside deviation only) |
| `max_drawdown_pct` | Peak-to-trough drawdown on cumulative equity curve (%) |
| `max_losing_streak`, `current_losing_streak` | Consecutive losses |
| `equity_curve` | `[{time, equity}]` — one point per closed trade |
| `by_strategy` | Per-strategy win rate, P&L, trade count |
| `by_symbol` | Per-symbol breakdown |
| `by_mode` | Per trading-type breakdown |
| `by_account` | Per account breakdown |
| `by_hour` | Win rate by UTC close hour (0–23) |
| `trade_quality` | `{high, medium, low}` — count by confidence tier |
| `failure_analysis` | Worst 5 symbols, worst 5 strategies, streak badges |

### `GET /analytics/regime/status`

Returns `{regimes: {symbol: label}}` — snapshot of all confirmed regime labels from the `regime_classifier` singleton.

### Trade Memory API Endpoints (under `/ai/memory/`)

| Endpoint | Description |
|---|---|
| `GET /ai/memory/stats` | Aggregate win rate, avg P&L, TP/SL counts, slippage |
| `GET /ai/memory/recent` | Last N trade outcomes |
| `GET /ai/memory/drift` | Page-Hinkley drift detection result |
| `GET /ai/memory/stability` | Rolling EV stability trend |
| `GET /ai/memory/stats/regime` | Performance breakdown per regime per strategy |
| `GET /ai/lstm/accuracy` | Live LSTM prediction accuracy vs actual outcomes |

---

## 5. Analytics Dashboard Page

**Location:** Sidebar → 📊 Analytics

### Layout

```
┌─ Account Tabs ─────────────────────────────────────┐
│  Paper  |  Live  |  All                            │
├─ Mode Tabs ──────────────────────────────────────────┤
│  All  |  Scalping  |  Day Trading  |  Swing         │
├─────────────────────────────────────────────────────┤
│  [Trades] [Win%] [P&L] [RR] [Sharpe] [Sortino]     │
│  [Max DD]  [Avg Win / Avg Loss]                     │
├─────────────────────────────────────────────────────┤
│  Equity Curve (SVG filled area)                     │
├─────────────────────────────────────────────────────┤
│  Trade Quality Bar (High | Medium | Low)            │
├─────────────────────────────────────────────────────┤
│  Regime Status Pills (live, per symbol)             │
├────────────────────┬────────────────────────────────┤
│  By Strategy table │  By Symbol table               │
├────────────────────┴────────────────────────────────┤
│  By Trading Mode (only when All Modes selected)     │
│  By Account (only when All Accounts selected)       │
├─────────────────────────────────────────────────────┤
│  Hour-of-Day Heatmap (24-cell UTC grid)             │
├─────────────────────────────────────────────────────┤
│  ⚠ Failure Analysis                                 │
│  Worst symbols | Worst strategies | Streak badges   │
└─────────────────────────────────────────────────────┘
```

### Visual Conventions

- **Equity curve** — filled SVG, green when final cumulative P&L ≥ 0, red when negative, zero baseline
- **Trade quality bar** — stacked horizontal: green = high confidence (≥ 0.75), yellow = medium (0.50–0.74), grey = low or unscored
- **Regime pills** — color-coded: green=bull, red=bear, blue=ranging-low-vol, orange=ranging-high-vol, purple=volatile-breakout, grey=quiet
- **Hour heatmap** — green cell ≥ 60% win rate, yellow ≥ 45%, red < 45%; hover shows trade count
- **Stat card colors** — Sharpe/Sortino ≥ 1.5 → green, ≥ 0.5 → yellow, < 0.5 → red; drawdown ≤ 8% → green, ≤ 15% → yellow; win rate ≥ 50% → green, ≥ 40% → yellow
- **Current losing streak** — failure panel badge turns red when streak ≥ 3

---

## 6. WebSocket Performance Monitor Alerts

A background task (`_performance_monitor` in `api/websocket/feed.py`) checks every 5 minutes and broadcasts alerts to all connected dashboard clients:

| Alert | Condition | Cooldown |
|---|---|---|
| Low win rate | Win rate < 40% for ≥ 10 trades in a mode | 1 hour |
| RL low risk factor | `risk_factor < 0.60` (agent reducing sizes) | 1 hour |
| LSTM degraded symbols | Live accuracy < 45% for any symbol | 1 hour |
| Win rate drift | Page-Hinkley severity = medium or high | 1 hour |
| Anchor accuracy drop | Live model accuracy dropped >8 pts below anchor | 4 hours |
| Symbol losing streak | 3+ consecutive losses for any symbol | 1 hour |
| Circuit breaker | Daily or weekly drawdown limit hit | Immediate (no cooldown) |
