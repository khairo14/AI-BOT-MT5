# EVOTRADE-AI — AI/ML Pipeline & Backtest System

**Last Updated:** April 2026

---

## Overview

The AI/ML system has three learning loops that work together, fed by both live trade outcomes and the user-facing backtest engine.

```text
MT5 Historical Bars
        │
        ├──────────────────────────────────────┐
        ▼                                      ▼
┌──────────────────┐                  ┌──────────────────┐
│  Param Optimizer │                  │  LSTM Retrainer  │
│  run_optimizer.py│                  │  run_retrain.py  │
│  (12 CPU workers)│                  │  (GPU/CPU)       │
└────────┬─────────┘                  └────────┬─────────┘
         │ config/optimized_params.json        │ ai/models/*.pt
         │                                     │
         └──────────────┬──────────────────────┘
                        │ (both complete)
                        ▼
                ┌───────────────────┐
                │  RL Bootstrap     │
                │  run_rl_bootstrap │
                │  (seeds Q-tables  │
                │  from backtests   │
                │  + LSTM scores)   │
                └───────────┬───────┘
                            │ ai/data/rl_qtable_*_paper.json
                            ▼
                    Live / Paper Trading
                            │
                Live trade closes → trade_memory.jsonl
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
      ┌──────────────┐         ┌──────────────────┐
      │   RL Agent   │         │  LSTM Auto-retrain│
      │  real-time   │         │  (every 20 trades │
      │  Q-update    │         │  or stale / loss  │
      └──────────────┘         │  streak trigger)  │
                               └──────────────────┘
```

---

## Correct Execution Order

Running the AI/ML pipeline for the first time (or after a full data reset) follows this sequence:

```text
Step 1 — Run in parallel (fully independent, no shared resources):
    Terminal 1:  python ai/run_retrain.py             # day_trading + swing LSTMs
                 python ai/run_retrain_scalping.py    # scalping LSTMs (after run_retrain.py)
    Terminal 2:  python ai/run_optimizer.py           # all strategies × all symbols

Step 2 — Only after BOTH complete:
    python ai/run_rl_bootstrap.py
```

**Why retrain must complete before bootstrap:**

- `run_rl_bootstrap.py` runs backtests with `use_ai_filters=True`
- This calls `predictor.predict()` at every signal bar
- Without trained models, every `conf_score = 0.0` → bootstrap fallback uses uniform `0.55` for all trades
- RL Q-table learns from identical `avg_conf=0.55` on every trade — cannot learn which confidence levels predict wins vs losses
- The resulting `conf_thresh` converges to an arbitrary value, not a meaningful one

**Why optimizer should complete before bootstrap (soft dependency):**

- The backtester calls `optimizer.get_params(strategy, symbol)` at the start of each run
- Without optimized params, strategies use code defaults and may fire different signal patterns than they will live
- RL calibrates to the wrong signal frequency and win rate
- Acceptable if optimizer partially complete; best if fully complete

**Why retrain and optimizer can run simultaneously:**

- Optimizer: reads OHLCV from MT5, writes `config/optimized_params.json`. Uses zero LSTM.
- Retrain: reads OHLCV from MT5, writes `ai/models/`. Uses zero optimizer.
- No shared write targets, no race conditions.

---

## 1. LSTM Price Predictor

See [07-ai-capabilities.md](./07-ai-capabilities.md) — Section 1 for full detail.

**Summary of retraining behaviour:**

- Manual: "Retrain" or "Retrain All" on the AI/ML Brain page
- Auto trigger 1: every 20th closed live/paper trade per symbol×mode
- Auto trigger 2: model stale > 7 days AND ≥ 10 trades
- Auto trigger 3: 8 consecutive losses for that symbol×mode
- 2-minute dedup cooldown prevents burst triggers from simultaneous multi-position closes

---

## 2. RL Agent

See [07-ai-capabilities.md](./07-ai-capabilities.md) — Section 4 for full detail.

**Bootstrap dependency summary:**

- Hard dependency on LSTM models (conf_score is meaningless without them)
- Soft dependency on optimizer (win rate more representative with optimized params)
- Always wait for both before running bootstrap

---

## 3. Parameter Optimizer

See [07-ai-capabilities.md](./07-ai-capabilities.md) — Section 6 for full detail.

**Summary of how it reads/writes:**

- Reads: `symbols.json`, `strategies.json`, OHLCV from MT5
- Writes: `config/optimized_params.json` (atomic write, 5-version archive)
- The backtester reads `optimized_params.json` automatically at the start of each run via `optimizer.get_params()`

---

## 4. Trade Memory

See [07-ai-capabilities.md](./07-ai-capabilities.md) — Section 7 for full detail.

**Sources:**

| Source | Written by | Mode tag |
| --- | --- | --- |
| Live trades | `signal_bus._poll_outcome()` | `"live"` |
| Paper trades | `signal_bus._poll_outcome()` or paper ledger sync | `"paper"` |
| Backtest simulations | `api/routes/backtest.py` | `"backtest"` |

---

## 5. User-Facing Backtest System

### What it is

A walk-forward strategy simulation on real MT5 historical OHLCV bars. Validates strategy historical performance before or after deploying live. No real orders are placed — purely historical simulation.

### How a backtest run flows

```text
User selects: symbol + strategy + mode + bars + balance + risk % + use_ai_filters
        │
        ▼
POST /backtest/run
        │
        ├── Fetch primary TF OHLCV from MT5
        ├── Fetch secondary TF OHLCV (for multi-TF strategies)
        │
        ├── Walk-forward simulation:
        │     For each bar i:
        │       Load optimized params for strategy×symbol
        │       Run strategy on bars[0..i] (rolling window, not growing slice)
        │       If use_ai_filters:
        │         Score with SignalScorer (LSTM + R:R + trend + volume)
        │         Apply RL gate (conf_thresh check)
        │         Apply RL risk_factor to lot sizing
        │       If signal fires → simulate SL/TP hit bar-by-bar
        │       Conservative: SL counted first when both hit same bar
        │       Record trade (entry, exit, outcome, pnl_pct, conf_score, equity)
        │
        ├── Compute metrics (win rate, profit factor, max DD, Sharpe, Sortino, etc.)
        │
        ├── Save full result as JSON: data/backtest_history/{run_id}.json
        ├── Append summary to index: data/backtest_history/_index.jsonl
        │
        └── If total_trades ≥ 30 → trigger param optimizer on same OHLCV bars
```

### Backtest timeframe mapping

| Strategy | Primary TF | Secondary TF |
| --- | --- | --- |
| `ema_scalp` | M1 | M5 (bias) |
| `bb_squeeze` | M5 | — |
| `vwap_reversion` | M5 | — |
| `macd_ema_trend` | M15 | H1 (trend) |
| `rsi_divergence` | M30 | H1 (trend) |
| `sr_breakout` | H1 | — |
| `ema_trend_rider` | H1 | H4 + D1 |
| `fibonacci_rsi` | H4 | — |
| `weekly_breakout` | H4 | D1 |

Secondary dataframes are time-sliced at bar `i` during walk-forward simulation — no lookahead bias.

### Result metrics

| Metric | Description |
| --- | --- |
| `win_rate` | Fraction of trades that were profitable |
| `profit_factor` | Gross wins / gross losses |
| `max_drawdown_pct` | Peak-to-trough on equity curve (%) |
| `sharpe_ratio` | Annualised Sharpe (per-TF annualisation factor applied) |
| `sortino_ratio` | Annualised Sortino (downside deviation only) |
| `total_pnl_pct` | Total % return from initial balance |
| `avg_rr` | Average realised R-multiple |
| `expectancy_pct` | Expected return per trade |
| `ai_filters_applied` | True when LSTM + RL gate were active |
| `avg_confidence` | Mean AI confidence score across accepted trades |
| `equity_curve` | `[{time, equity}]` — one point per closed trade |

### Dashboard — Backtest page

- Symbol grouped `<select>` per mode matching the full symbol scope
- Run results: equity curve, stat cards, paginated trade log
- History panel: all saved runs (newest first, paginated), with mini sparkline, metrics, load/delete buttons
- History capped at 200 runs (oldest pruned automatically)

### API endpoints

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/backtest/run` | Run simulation, save result, trigger optimizer |
| `GET` | `/backtest/strategies` | List available strategies per mode |
| `GET` | `/backtest/history` | Paginated run list (filters: symbol, strategy, mode) |
| `GET` | `/backtest/history/{id}` | Full run data including trade log |
| `DELETE` | `/backtest/history/{id}` | Delete a saved run |
| `GET` | `/backtest/journal/replay` | Replay closed journal trades through current params |

---

## 6. Analytics System

**Endpoint:** `GET /analytics/performance` (filters: account, trading_type, limit)

Returns comprehensive performance data from `data/trade_journal.jsonl`:

| Metric Group | Fields |
| --- | --- |
| Counts | total_trades, wins, losses, win_rate |
| P&L | total_profit, avg_profit, avg_win, avg_loss, avg_rr |
| Risk-adjusted | sharpe_ratio, sortino_ratio, max_drawdown_pct |
| Streaks | max_losing_streak, current_losing_streak |
| Time series | equity_curve |
| Breakdowns | by_strategy, by_symbol, by_mode, by_account, by_hour |
| Quality | trade_quality (high/medium/low confidence tier counts) |
| Failure | failure_analysis (worst symbols, worst strategies, streak badges) |

**Endpoint:** `GET /analytics/regime/status` — current confirmed regime label for all classified symbols.

### Analytics Dashboard page

- Account tabs: Paper / Live / All
- Mode tabs: All / Scalping / Day Trading / Swing
- Equity curve: filled SVG, green when cumulative P&L ≥ 0, red when negative
- Trade quality bar: stacked (green = high ≥ 0.75, yellow = medium 0.50–0.74, grey = low)
- Regime pill bar: color-coded by label (green=bull, red=bear, blue=ranging-low, orange=ranging-high, purple=breakout, grey=quiet)
- Hour-of-day heatmap: 24-cell UTC grid with win-rate colors
- Stat card colors: green/yellow/red based on health thresholds (Sharpe ≥ 1.5 green, drawdown ≤ 8% green)

---

## 7. The Feedback Loop (Summary)

```text
Phase 1 — Setup (offline, one-time):
  run_retrain.py + run_retrain_scalping.py  [parallel with optimizer]
  run_optimizer.py                          [parallel with retrain]
  → both complete →
  run_rl_bootstrap.py

Phase 2 — Live adaptation (automatic):
  Every trade close:
    → trade_memory.record()
    → RL.observe() (immediate Q-table update)
    → Every 20 trades: LSTM auto-retrain
    → Win rate < 45% + 30 trades: optimizer auto-trigger

Phase 3 — Periodic maintenance (weekly recommended):
  Re-run run_retrain.py (day_trading + swing)
  Re-run run_retrain_scalping.py
  Re-run run_optimizer.py (skips already-done 2yr jobs)
  Re-run run_rl_bootstrap.py (after both complete)
```

The two phases compound — the longer the bot runs, the more refined its parameters, LSTM models, and RL thresholds become.
