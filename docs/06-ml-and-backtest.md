# EVOTRADE-AI — AI/ML Pipeline & Backtest System

## Overview

The AI/ML system has three learning loops that work together. The backtest system feeds into all three.

```
MT5 Historical Bars
        │
        ▼
┌──────────────────┐     ┌───────────────────────┐
│  Param Optimizer │────▶│  Strategy Parameters  │
│  (grid search)   │     │  opt_params.json       │
└──────────────────┘     └───────────────────────┘
        ▲
        │ auto-triggers after ≥30 simulated trades
        │
┌──────────────────┐
│  Backtest Engine │
│  (user-facing)   │────▶ trade_memory.jsonl (source=backtest)
└──────────────────┘             │
                                 ▼
Live/Paper Trade Closes ───▶ trade_memory.jsonl
                                 │
                    ┌────────────┴────────────┐
                    ▼                         ▼
           ┌──────────────┐         ┌──────────────────┐
           │   RL Agent   │         │  LSTM Predictor  │
           │  (Q-table)   │         │  (auto-retrain   │
           │  real-time   │         │   after 20 new   │
           │  update      │         │   live trades)   │
           └──────────────┘         └──────────────────┘
```

---

## 1. LSTM Price Predictor (`ai/predictor.py`)

**Purpose:** Predict directional price movement for a symbol+trading_type pair. Returns a 0–1 confidence score — 0.5 is neutral, >0.65 is a strong directional signal.

**Training data:** Raw OHLCV from MT5 at the natural timeframe for each mode:

| Mode | Timeframe |
|---|---|
| Scalping | M5 |
| Day Trading | H1 |
| Swing | H4 |

**When it retrains:**
- Manually triggered from the AI/ML Brain page ("Train" button)
- Automatically triggered after 20+ new live/paper trade outcomes accumulate (threshold in `signal_bus.py`)

**Persistence:** Trained model weights saved to `ai/models/`. Each symbol+type has its own model file.

**Dashboard:** AI/ML Brain page → "LSTM Models" section — shows last trained time, accuracy, and bars used per symbol.

---

## 2. RL Agent (`ai/rl_agent.py`)

**Purpose:** Learns from closed trade outcomes and adjusts two variables:
- **Confidence threshold** — minimum LSTM confidence score required before a signal is acted on
- **Risk factor** — multiplier applied to the base risk % per trade

**Algorithm:** Simple Q-table (tabular RL) — fast, interpretable, no GPU needed.

**State:** Current market regime derived from recent win rate + volatility bucket.

**Update:** Called immediately when any trade closes (`rl_manager.on_trade_closed()`). The agent receives a reward based on P&L and outcome type (tp_hit = positive, sl_hit = negative), then updates the Q-table via Bellman equation.

**Persistence:** Q-table saved to `ai/data/rl_qtable_{trading_type}.json` after each update.

**One agent per trading type** — scalping, day_trading, swing each have independent Q-tables because their market dynamics differ.

**Dashboard:** AI/ML Brain page → "RL Agent" section — shows current thresholds and risk factors per mode. Reset button available.

---

## 3. Parameter Optimizer (`ai/param_optimizer.py`)

**Purpose:** Find the best parameters for each strategy+symbol pair (e.g., EMA 8/21 vs EMA 9/26, RSI threshold 55 vs 60).

**Method:** Walk-forward grid search — at each bar, run the strategy on all prior bars, simulate the trade, measure win rate + avg RR. Repeat for every param combo. Best combo by composite score is saved.

**When it triggers:**
1. **Manually** — "Run Optimizer" button on the AI/ML Brain page
2. **After 20+ live/paper trade outcomes** accumulate for a strategy+symbol (auto from `signal_bus.py`)
3. **After a user-facing backtest with ≥30 simulated trades** — auto-triggered from `backtest.py` using the same OHLCV bars already fetched for the simulation

**Cooldown:** Won't re-optimize the same strategy+symbol within `BACKTEST_COOLDOWN_HOURS` (default 24h) to avoid thrashing.

**Persistence:** Best params saved to `ai/data/opt_params.json`. Used by the strategy engine at signal generation time.

**Dashboard:** AI/ML Brain page → "Parameter Optimizer" section — shows last run time, best params, and score per strategy+symbol.

---

## 4. Trade Memory (`ai/trade_memory.py`)

**Purpose:** Persistent store of every closed trade outcome — the central data source for both the RL agent and the auto-optimizer trigger.

**Sources:**
| Source | Written by | Notes |
|---|---|---|
| Live trades | `signal_bus._poll_outcome()` | Full data: confidence, pips, money P&L, volume |
| Paper trades | `signal_bus._poll_outcome()` | Same as live — uses XM Demo account |
| Backtest simulations | `api/routes/backtest.py` | `source="backtest"` in `extra` field; `confidence=0.5` (no live score); `profit` = `pnl_pct` |

**Storage:**
- **Disk:** `ai/data/trade_memory.jsonl` — append-only, unlimited, one JSON object per line
- **RAM:** Rolling buffer of last 5,000 entries for fast reads by the RL agent

**Key fields per entry:**

| Field | Description |
|---|---|
| `ticket` | MT5 ticket number (0 for backtest entries) |
| `symbol` | e.g. `EURUSD` |
| `strategy` | e.g. `ema_scalp` |
| `trading_type` | `scalping` \| `day_trading` \| `swing` |
| `direction` | `BUY` \| `SELL` |
| `confidence` | LSTM score at signal time (0–1) |
| `outcome` | `tp_hit` \| `sl_hit` \| `manual_close` \| `timeout` |
| `profit_pct` | % of account balance P&L |
| `extra.source` | `"live"`, `"paper"`, or `"backtest"` |

**Dashboard:** AI/ML Brain page → "Trade Memory" section — shows win rate, avg P&L, TP/SL hit counts, and last N entries.

---

## 5. User-Facing Backtest System

### What it is

A walk-forward strategy simulation run on real MT5 historical bars. Used to validate and audit a strategy's historical performance before or after deploying it live.

### What it is NOT

It is not a paper trade simulation — it runs purely on historical bar data, no real orders are placed. The strategy sees only bars up to the current simulation point (no look-ahead bias).

### How a backtest run flows

```
User selects: symbol + strategy + mode + bars + balance + risk %
        │
        ▼
POST /backtest/run
        │
        ├── Fetch OHLCV from MT5 (real historical bars)
        │
        ├── Walk-forward simulation:
        │     For each bar i:
        │       Run strategy on bars[0..i]
        │       If signal fires → simulate SL/TP hit bar-by-bar
        │       Record trade (entry, exit, outcome, P&L, equity)
        │
        ├── Compute metrics (win rate, profit factor, max DD, Sharpe, etc.)
        │
        ├── Save full result as JSON: data/backtest_history/{run_id}.json
        ├── Append summary to index: data/backtest_history/_index.jsonl
        │
        ├── Feed each simulated trade → trade_memory.jsonl  ← RL agent learns
        │
        └── If total_trades ≥ 30 → trigger param optimizer  ← strategy params update
```

### Timeframes used

| Mode | Backtest Timeframe |
|---|---|
| Scalping | M5 |
| Day Trading | H1 |
| Swing | H4 |

### Saved run schema (summary fields in index)

| Field | Description |
|---|---|
| `id` | UUID run identifier |
| `run_at` | ISO timestamp (UTC) |
| `symbol` | e.g. `EURUSD` |
| `strategy` | e.g. `ema_scalp` |
| `trading_type` | `scalping` \| `day_trading` \| `swing` |
| `timeframe` | e.g. `M5` |
| `bars_tested` | Number of OHLCV bars used |
| `total_trades` | Simulated trades fired |
| `win_rate` | 0–1 |
| `profit_factor` | Gross wins / gross losses |
| `max_drawdown_pct` | Worst peak-to-trough % |
| `sharpe_ratio` | Annualised Sharpe |
| `total_pnl_pct` | Total % return from initial balance |

### Dashboard — Backtest page

- **Symbol select:** Grouped `<select>` per mode — same symbol universe as the trading chart pages
- **Results:** Equity curve, 10 stat cards, full paginated trade log
- **History panel:** Table of all past runs (newest first, paginated 15/page) with mini sparkline, metrics, load button, delete button
- Clicking a history row loads that full run result inline — no page refresh needed

### API endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/backtest/run` | Run simulation, save result, feed ML |
| `GET` | `/backtest/strategies` | List available strategies per mode |
| `GET` | `/backtest/history` | Paginated run list (filters: symbol, strategy, mode) |
| `GET` | `/backtest/history/{id}` | Full run data including trade log |
| `DELETE` | `/backtest/history/{id}` | Delete a saved run |

---

## 6. The Feedback Loop (summary)

The system implements the two-phase design requested:

**Phase 1 — Backtest first:**
When you run a backtest, the optimizer runs on the same bars if enough trades fired. This finds optimal strategy parameters before going live.

**Phase 2 — Live/paper refine:**
Every real trade that closes feeds the RL agent immediately and accumulates in trade memory. After 20 new outcomes, the LSTM auto-retrains. After 20+ outcomes for a strategy+symbol, the optimizer may re-run to refine parameters based on actual market conditions.

The two phases compound — the longer the bot runs, the more refined its parameters become.
