# EVOTRADE-AI — System Architecture

**Last Updated:** April 2026 | **Status:** Production — paper-trading active, live-capable

---

## Overview

A full-stack AI-powered trading bot connected to XM via MetaTrader 5. It supports scalping, day trading, and swing trading — each with its own dashboard, strategy engine, and symbol scope. The system operates in both paper (demo) and live modes, with manual confirmation or fully automatic execution. A complete AI/ML pipeline — LSTM price prediction, Q-learning reinforcement learning, regime classification, and walk-forward parameter optimisation — adapts all three trading types in real time.

---

## Broker & Account Setup

| Item | Detail |
|---|---|
| Broker | XM |
| Platform | MetaTrader 5 (MT5) |
| Paper Trading Account | XM Demo MT5 account |
| Live Trading Account | XM Live MT5 account |
| Account Switch | In-app toggle — switches active MT5 connection without restart |

> Paper trading uses a real XM Demo account. All paper trades are recorded on XM's servers with full history, equity curve, and export capability. This is not a local simulation — it is a full demo account treated as a staging environment.

---

## System Layers

### Layer 1 — MT5 Connection (Python + MQL5)

- `MetaTrader5` Python library — account info, OHLCV data, trade history, position management
- **MQL5 Expert Advisor (EA)** — installed inside the MT5 terminal for **sub-millisecond order execution on scalping** via heartbeat-based file-protocol bridge (`engine/ea_bridge.py`). Python generates signals; the EA places orders natively. Falls back transparently to Python execution if the EA is inactive.
- Day trading and swing trading use Python execution directly (latency is not critical at M15–D1)
- `MT5Client._lock` (RLock) serialises all MT5 SDK calls — the library is not thread-safe. All callers acquire this lock including the runner loop, order manager, poll outcome tasks, and WebSocket tick feed.
- Auto-reconnect: `_mt5_watchdog` background task checks connection every 30 s and calls `client.reconnect()` on drop. The strategy runner loop also checks and reconnects before each tick.

### Layer 2 — Strategy Engine (Python)

- Modular design: every strategy is an independent Python class inheriting from `BaseStrategy`
- Each strategy implements: signal generation, entry price, SL/TP calculation, and indicator computation
- Strategies grouped by trading type — 3 per type = **9 strategies total**
- Strategy selector: configurable per symbol per mode via `config/strategies.json`
- Bar-close guard in `runner_loop.py`: each mode only runs when a new primary-TF bar has closed since the last scan (2-bar OHLCV fetch used for comparison). Reduces MT5 lock contention by ~92% for scalping.
- Full strategy detail: see [03-strategies.md](./03-strategies.md)

### Layer 3 — AI / ML Engine (Python)

- **LSTM Price Predictor** (`ai/predictor.py`) — trained per symbol×type on OHLCV data; returns a 0–1 directional confidence score. 7 input features, 2-layer LSTM hidden-64, mini-batch shuffled training with label smoothing and class balancing. 3-version archive + frozen anchor model for rollback.
- **Signal Scorer** (`ai/signal_scorer.py`) — blends LSTM probability (30%), R:R quality (35%), EMA50/200 trend alignment (20%), and volume confirmation (15%) into a single 0–1 confidence score. Weights are regime-aware and configurable per trading type.
- **Regime Classifier** (`engine/regime_classifier.py`) — classifies market condition (trending_bull/bear, ranging_low/high_vol, volatile_breakout, quiet) using ADX + ATR% + EMA alignment with 3-bar hysteresis and per-asset-class thresholds. Provides a single authoritative label to all downstream components.
- **Reinforcement Learning Agent** (`ai/rl_agent.py`) — tabular Q-learning (162 states), one agent per trading type, paper/live isolated. Dynamically tunes confidence threshold (0.40–0.85) and risk factor (0.50–1.50) from closed trade outcomes. Bootstrapped from backtests before live deployment.
- **Parameter Optimizer** (`ai/param_optimizer.py`) — walk-forward grid search with 75/25 train/val split, spread-deducted simulation, per-regime best params, atomic file writes, and version archive. Auto-triggers on win-rate degradation.
- **Trade Memory** (`ai/trade_memory.py`) — append-only JSONL; rolling 10,000-entry RAM buffer. Stores every closed trade with confidence, regime, outcome type, slippage, and mode tag. Powers drift detection, EV stability tracking, LSTM accuracy monitoring, and RL training.
- ML frameworks: `PyTorch`, `scikit-learn`, `pandas`, `numpy`

### Layer 4 — Risk Manager (Python)

- Per-trade risk: configurable % of account balance (default 1%, hard ceiling 2%)
- Mandatory stop-loss on every trade — enforced in `order_manager.py`, not just config
- Lot sizing: `risk_amount / (sl_ticks × tick_value)` — MT5-native formula; floor-rounded to `lot_step`; warns when broker minimum lot exceeds intended risk
- Volatility-adjusted sizing: ATR% vs baseline scales lot down up to 50% during extreme volatility regimes
- Regime-based lot reduction: `volatile_breakout` → ×0.75, `ranging_high_vol` → ×0.85, etc.
- RL risk factor: RL agent multiplies base lot by 0.50–1.50 based on learned state
- Drawdown circuit breakers: daily (default 5%) and weekly (default 10%), UTC-based reset, year-boundary safe, disk-persisted across restarts
- Consecutive loss pause: per-mode, configurable N losses and cooldown hours
- Concurrent position limits: per-mode and per-symbol caps
- Correlation guard: blocks same-direction positions in 8 asset groups (USD pairs, crypto, gold/silver, oil, US indices, EU indices, tech stocks) within the same trading mode
- SL/TP reanchoring: order manager shifts SL/TP to live fill price to prevent invalid-stop broker rejections
- Broker minimum stop enforcement: `trade_stops_level × point` floor applied before sending
- Live spread gate: blocks entry when current spread exceeds per-mode `max_spread_pips`
- Session filter: market-hours enforcement per instrument category with full US market holiday calendar (NYSE/NASDAQ)
- News filter: Forex Factory calendar (this-week + next-week attempted via date-param URL), DST-aware ET→UTC conversion, earnings blackout for stock CFDs
- Signal expiry: pending manual signals expire after one bar's worth of time per timeframe
- Full configuration detail: see [04-risk-and-configuration.md](./04-risk-and-configuration.md)

### Layer 5 — Paper Trading Mode

- Activated by switching the in-app account selector to the XM Demo account
- Identical code path to live trading — same strategies, same risk manager, same AI
- All trades recorded on XM's demo server with full deal history
- `PaperTradeEngine.sync_positions()` runs every 60 s, syncing P&L, SL, and TP (including ATR trail and breakeven moves applied by the outcome poller)
- Toggle between demo/live without restarting the application; RL agents reload for the new mode

### Layer 6 — Backend API (Python / FastAPI)

- REST + WebSocket server with optional API key header guard
- Route modules in `api/routes/`:
  - `account.py` — balance, equity, margin, mode switch, reconnect
  - `signals.py` — pending signal queue, approve/reject with expiry check
  - `trades.py` — open/close positions, OHLCV fetch, trade journal
  - `config.py` — atomic read/write of all config files with full schema validation and a `GET /config/validate` health endpoint
  - `risk.py` — risk status, news status, session status, circuit breaker toggle, manual reset
  - `ai.py` — LSTM train/status/rollback/calibrate, RL status/reset, optimizer status/trigger, trade memory stats/drift/stability/regime
  - `backtest.py` — walk-forward backtest, saved run history (CRUD), journal replay
  - `analytics.py` — comprehensive performance analytics (Sharpe, Sortino, drawdown, equity curve, regime status)
- `api/signal_bus.py` — full signal lifecycle: queue → dedup → confidence gate → RL gate → correlation guard → order → trail/breakeven → outcome polling → trade memory → RL update → auto-retrain trigger → auto-optimizer trigger
- `api/runner_loop.py` — background asyncio loop with per-mode bar-close guard, weekend gap protection, paper sync, and scanner config hot-reload
- WebSocket: `/ws/feed` — live price ticks, open position snapshots, signal updates, performance alerts, circuit breaker events

### Layer 7 — Web Dashboard (Next.js / TypeScript / Tailwind CSS)

- Pages: **Overview**, **Scalping**, **Day Trading**, **Swing**, **Backtest**, **AI / ML Brain**, **Analytics**, **Notifications**, **Settings**, **Guide**
- Each trading page: TradingView Lightweight Chart, trade panel, signal queue, strategy config, active positions
- Manual confirmation mode: signal popup → Approve / Reject → order fires (with expiry check)
- Auto mode: order fires immediately on signal
- In-app notification system: WebSocket-driven toasts for signals, fills, SL/TP hits, drawdown alerts, circuit breaker events, LSTM degradation, drift alerts
- **Overview page**: balance/equity cards, trading mode tiles, open positions table, account performance panel, bot trade journal with filtering
- **Analytics page**: equity curve, Sharpe/Sortino/drawdown stat cards, trade quality bar, regime pill bar, per-strategy/symbol/mode/hour breakdowns, failure analysis
- **Backtest page**: run walk-forward simulations, equity curve + paginated trade log, save/load/delete run history
- **AI / ML Brain page**: LSTM train status + trigger, RL agent state + reset, parameter optimizer status + trigger, trade memory stats
- Account switcher: Demo ↔ Live (with open-position guard)

---

## Tech Stack

| Component | Technology |
|---|---|
| MT5 Connection | `MetaTrader5` Python library (Windows only) |
| Scalping Order Execution | MQL5 Expert Advisor + Python EA bridge fallback |
| Data & Indicators | `pandas`, `numpy` |
| Strategy Engine | Python classes (modular, 9 strategies) |
| AI / Price Prediction | `PyTorch` LSTM — `ai/predictor.py` |
| Reinforcement Learning | Tabular Q-learning — `ai/rl_agent.py` |
| Regime Classification | ADX + ATR + EMA — `engine/regime_classifier.py` |
| Signal Confidence Scoring | Weighted blend — `ai/signal_scorer.py` |
| Parameter Optimisation | Walk-forward grid search — `ai/param_optimizer.py` |
| Trade Memory | Append-only JSONL — `ai/data/trade_memory.jsonl` |
| Backend API | `FastAPI` + `asyncio` WebSocket |
| Frontend | Next.js + TypeScript + Tailwind CSS |
| Charts | TradingView Lightweight Charts |
| Real-time Feed | WebSocket (MT5 ticks → FastAPI → UI) |
| Config Persistence | Atomic JSON/JSONL files — no database required |
| Auth | Optional API key header (configurable in `app.json`) |
| Deployment | `start.bat` / `stop.bat` — Windows-native launcher |

---

## Project Directory Structure

```
EVOTRADE-AI/
├── engine/
│   ├── mt5_client.py             # MT5 connection, account, OHLCV, positions
│   ├── order_manager.py          # Place, modify, partial-close, close orders
│   ├── paper_trade.py            # Demo account ledger + SL/TP sync
│   ├── risk_manager.py           # Sizing, drawdown, circuit breaker, correlation
│   ├── strategy_runner.py        # Dispatcher: regime → params → signal → score → gate
│   ├── backtester.py             # Walk-forward backtest engine
│   ├── regime_classifier.py      # 6-label market regime classifier
│   ├── news_filter.py            # Forex Factory news + earnings blackout
│   ├── session_filter.py         # Session-aware signal suppression + holiday calendar
│   ├── trade_journal.py          # Append-only JSONL trade journal
│   ├── account_store.py          # Paper/live mode persistence
│   └── strategies/
│       ├── base_strategy.py
│       ├── scalping/             # ema_scalp, bb_squeeze, vwap_reversion
│       ├── day_trading/          # macd_ema_trend, sr_breakout, rsi_divergence
│       └── swing/                # ema_trend_rider, fibonacci_rsi, weekly_breakout
├── ai/
│   ├── predictor.py              # LSTM price predictor (per symbol×mode)
│   ├── rl_agent.py               # Q-table RL agent + RLAgentManager
│   ├── signal_scorer.py          # Confidence scorer (LSTM + RR + trend + volume)
│   ├── param_optimizer.py        # Walk-forward grid search parameter optimizer
│   ├── trade_memory.py           # Closed trade outcome store + analytics
│   ├── run_retrain.py            # Standalone LSTM retrainer (day_trading + swing)
│   ├── run_retrain_scalping.py   # Standalone LSTM retrainer (scalping)
│   ├── run_optimizer.py          # Standalone multiprocessing optimizer (12 workers)
│   ├── run_rl_bootstrap.py       # Seed Q-tables from backtest outcomes
│   ├── reset_scalping_rl.py      # CLI: reset scalping Q-table safely
│   ├── models/                   # LSTM .pt + .pkl + _meta.json per symbol×mode
│   │   └── versions/             # Last 3 model versions per key (rollback)
│   └── data/
│       ├── trade_memory.jsonl    # Permanent trade outcome log
│       ├── rl_qtable_*_paper.json
│       ├── rl_qtable_*_live.json
│       └── optimizer_status.json
├── api/
│   ├── main.py                   # FastAPI app, lifespan, watchdog tasks
│   ├── signal_bus.py             # Full signal lifecycle (queue → execute → poll → record)
│   ├── runner_loop.py            # Background strategy loop with bar-close guard
│   ├── dependencies.py           # API key guard, MT5 client injection
│   └── routes/
│       ├── account.py, trades.py, signals.py
│       ├── config.py             # Atomic reads/writes + schema validation
│       ├── risk.py, ai.py, backtest.py, analytics.py
│   └── websocket/
│       └── feed.py               # Tick feed + performance monitor alerts
├── mql5/
│   └── AIBotScalper.mq5          # Expert Advisor for scalping execution
├── config/
│   ├── app.json                  # Execution mode, AI flags, scorer weights, regime weights
│   ├── risk.json                 # Risk parameters, drawdown limits, news/session filters
│   ├── symbols.json              # Symbol list per trading type
│   ├── strategies.json           # Active strategies per mode/symbol
│   ├── scanner.json              # Per-mode scanner enabled/symbols override
│   ├── optimized_params.json     # Best params per strategy×symbol (+ by_regime)
│   └── account_mode.json         # Last active mode (paper/live)
├── data/
│   ├── trade_journal.jsonl       # Append-only open/close event log
│   ├── backtest_history/         # One JSON per run + _index.jsonl
│   ├── regime_state.json         # Persisted hysteresis state per symbol
│   └── risk_state.json           # Persisted circuit breaker state
├── dashboard/                    # Next.js frontend
│   └── app/
│       ├── overview, scalping, day-trading, swing
│       ├── analytics, backtest, ml, notifications, settings, guide
├── docs/                         # Documentation (this set of files)
├── start.bat                     # 1-click launcher: API + dashboard
└── stop.bat                      # Kills all processes
```

---

## Build Phases

| Phase | Deliverable | Status |
|---|---|---|
| 1 | MT5 connection, account info, OHLCV fetch, place/close orders | ✅ Done |
| 2 | Strategy engine + 9 strategies (3 per type) + paper trading | ✅ Done |
| 3 | FastAPI backend + WebSocket live price feed | ✅ Done |
| 4 | Next.js dashboard — 3 trading dashboards + charts | ✅ Done |
| 5 | Execution loop, signal bus, auto/manual mode, browser notifications | ✅ Done |
| 6 | LSTM price prediction + signal confidence scoring | ✅ Done |
| 7 | RL agent training loop | ✅ Done |
| 8 | Risk manager hardening + news filter + drawdown circuit breaker | ✅ Done |
| 9 | Demo ↔ Live account switching + full paper trade sync | ✅ Done |
| 10 | `start.bat` / `stop.bat` — 1-click Windows launcher | ✅ Done |
| 11 | Collapsible sidebar, custom MA overlay, sub-chart alignment | ✅ Done |
| 12 | AI/ML Brain page — LSTM, RL, optimizer, trade memory | ✅ Done |
| 13 | Walk-forward backtest engine + backtest page with history | ✅ Done |
| 14 | Backtest → trade memory feed + auto optimizer trigger | ✅ Done |
| 15 | Regime classifier + regime-aware scoring + extended correlation guard | ✅ Done |
| 16 | Analytics API + Analytics dashboard page | ✅ Done |
| 17 | Full codebase audit (53+ findings resolved across 6 rounds) | ✅ Done |
| 18 | April 2026 audit: 14 additional bugs fixed, 11 limitations addressed | ✅ Done |

---

## Key Constraints

- `MetaTrader5` Python library **only runs on Windows** — the engine runs locally on Windows; no Docker, no VM
- MQL5 EA runs **inside the MT5 terminal** — MT5 must be open for scalping EA execution; Python strategies can run headlessly for day/swing trading
- Paper trading uses the **XM Demo account** — trades appear on XM's servers as real demo trades
- Stock CFDs (TSLA, NVDA, etc.) only trade during **exchange hours** — the bot enforces this automatically
- Futures instruments with expiry dates (US100-JUN26, etc.) are **excluded** — Cash instruments only
- The optimizer and retrain scripts are CPU/GPU intensive — run them offline (not while the live bot is active on a single-core machine). On multi-core machines they share MT5 lock time with the runner loop via the bar-close guard.
