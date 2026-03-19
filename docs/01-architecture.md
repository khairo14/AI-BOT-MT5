# EVOTRADE-AI — System Architecture

## Overview

A full-stack AI-powered trading bot connected to XM via MetaTrader 5. It supports scalping, day trading, and swing trading — each with its own dashboard, strategy engine, and symbol scope. The system operates in both paper (demo) and live modes, with manual confirmation or fully automatic execution.

---

## Broker & Account Setup

| Item | Detail |
|---|---|
| Broker | XM (already registered) |
| Platform | MetaTrader 5 (MT5) |
| Paper Trading Account | XM Demo MT5 account |
| Live Trading Account | XM Live MT5 account |
| Account Switch | In-app toggle — switches active MT5 connection |

> Paper trading uses a real XM Demo account. All paper trades are recorded on XM's servers with full history, equity curve, and export capability. This is not a local simulation — it is a full demo account treated as a staging environment.

---

## System Layers

### Layer 1 — MT5 Connection (Python + MQL5)

- `MetaTrader5` Python library — account info, OHLCV data, trade history, position management
- **MQL5 Expert Advisor (EA)** — installed inside the MT5 terminal for **order execution on scalping** (sub-millisecond latency vs. ~50–300ms Python latency)
- Python handles signal generation, AI, and dashboard communication
- MQL5 EA handles only order send/modify/close for scalping strategies where latency matters
- Day trading and swing trading use Python execution directly (latency is not critical at M15–D1)

### Layer 2 — Strategy Engine (Python)

- Modular design: every strategy is an independent Python class
- Each strategy implements: `signal()`, `entry()`, `exit()`, `risk()`
- Strategies are grouped by trading type (scalping / day / swing)
- 3 strategies per trading type = **9 strategies total**
- Strategy selector: configurable per symbol per trading type
- Full strategy detail: see [03-strategies.md](./03-strategies.md)

### Layer 3 — AI / ML Engine (Python)

- **LSTM Price Predictor** (`ai/predictor.py`) — trained per symbol+type on OHLCV data; returns a 0–1 directional confidence score used to filter weak signals
- **Reinforcement Learning Agent** (`ai/rl_agent.py`) — Q-table per trading type; learns from closed trade outcomes and adjusts confidence thresholds + risk sizing in real time
- **Parameter Optimizer** (`ai/param_optimizer.py`) — grid-searches strategy parameters (EMA periods, RSI thresholds, etc.) using walk-forward backtesting; auto-triggers after 20+ new trades or after a user-facing backtest with ≥ 30 simulated trades
- **Trade Memory** (`ai/trade_memory.py`) — append-only JSONL store (`ai/data/trade_memory.jsonl`); up to 5,000 outcomes in RAM, unlimited on disk. Sources: live trades, paper trades, and backtest simulations
- **Signal Scorer** (`ai/signal_scorer.py`) — per-signal confidence scoring using recent win-rate and RL agent state
- ML frameworks: `PyTorch`, `scikit-learn`, `pandas`, `numpy`

### Layer 4 — Risk Manager (Python)

- Per-trade risk: configurable % of account balance (default: 1%)
- Mandatory stop-loss on every trade — no trade opens without an SL
- Take-profit automatically computed from risk:reward ratio
- Drawdown circuit breaker: auto-pauses all trading if daily drawdown exceeds threshold (default: 5%)
- Max concurrent open trades per trading type (configurable, bot-managed by default)
- Position sizing: fixed fractional (default) or Kelly Criterion (optional)
- News filter: pauses trading during high-impact Forex Factory events
- Full configuration detail: see [04-risk-and-configuration.md](./04-risk-and-configuration.md)

### Layer 5 — Paper Trading Mode

- Activated by switching the in-app account selector to the XM Demo account
- Identical code path to live trading — same strategies, same risk manager, same AI
- All trades recorded on XM's demo server
- Toggle between demo/live without restarting the application

### Layer 6 — Backend API (Python / FastAPI)

- REST + WebSocket server; no auth layer (local-only deployment)
- Bridges the frontend dashboard with the trading engine
- Route modules in `api/routes/`:
  - `account.py` — balance, equity, margin, positions, trade history
  - `signals.py` — pending signals queue, confirm/reject
  - `trades.py` — open/close trades
  - `config.py` — read/write `config/*.json` settings
  - `risk.py` — risk parameter overrides
  - `ai.py` — LSTM train/status, RL status/reset, trade memory stats, param optimizer
  - `backtest.py` — walk-forward backtest, saved run history (CRUD)
- `api/signal_bus.py` — execution loop: strategy signal → confidence score → RL gate → order → outcome recording
- `api/runner_loop.py` — background polling loop; runs strategies on live ticks
- WebSocket: `ws/feed` — live price tick stream per symbol

### Layer 7 — Web Dashboard (Next.js 16 / TypeScript / Tailwind CSS)

- Pages: **Scalping**, **Day Trading**, **Swing Trading**, **Backtest**, **AI / ML Brain**, **Notifications**, **Settings**, **Guide**
- Each trading page has its own TradingView Lightweight Chart, trade panel, strategy config, and active positions
- Collapsible sidebar with persistent state
- Symbol grouped `<select>` per mode (matches exactly the symbol scope from `02-symbol-scope.md`)
- Manual confirmation mode: signal popup → Approve / Reject → order fires
- Auto mode: order fires immediately on signal
- In-app notification system: browser toasts for signals, fills, SL/TP hits, drawdown alerts
- **Backtest page**: run walk-forward strategy simulations, view equity curve + trade log (paginated), save/load/delete run history
- **AI / ML Brain page**: LSTM train status + trigger, RL agent state, param optimizer status + trigger, trade memory stats
- Account switcher: Demo ↔ Live
- Built-in trading reference guide (see [05-trading-reference-guide.md](./05-trading-reference-guide.md))

---

## Tech Stack

| Component | Technology |
|---|---|
| MT5 Connection | `MetaTrader5` Python library |
| Scalping Order Execution | MQL5 Expert Advisor (inside MT5 terminal) |
| Data & Indicators | `pandas`, `pandas-ta`, `numpy` |
| Strategy Engine | Python classes (modular) |
| AI / Price Prediction | `PyTorch` (LSTM) — `ai/predictor.py` |
| Reinforcement Learning | Custom Q-table — `ai/rl_agent.py` |
| Signal Confidence Scoring | Custom scorer — `ai/signal_scorer.py` |
| Parameter Optimization | Walk-forward grid search — `ai/param_optimizer.py` |
| Trade Memory | Append-only JSONL — `ai/data/trade_memory.jsonl` |
| Backend API | `FastAPI` + `WebSocket` |
| Task Queue | `asyncio` background tasks (built into FastAPI) |
| Frontend | `Next.js 16` + TypeScript + Tailwind CSS |
| Charts | TradingView Lightweight Charts |
| Real-time Feed | WebSocket (MT5 ticks → FastAPI → UI) |
| Persistence | Flat JSON/JSONL files — no database required |
| Auth | None (local-only deployment) |
| Deployment | `start.bat` / `stop.bat` — Windows-native launcher |

---

## Project Directory Structure

```
EVOTRADE-AI/
├── docs/                         # All documentation (you are here)
├── engine/
│   ├── mt5_client.py             # MT5 connection, account, data
│   ├── order_manager.py          # Place, modify, close orders (Python path)
│   ├── paper_trade.py            # Demo account trading logic
│   ├── risk_manager.py           # SL/TP, position sizing, circuit breaker
│   ├── news_filter.py            # High-impact news event checker
│   └── strategies/
│       ├── base_strategy.py      # Abstract base class
│       ├── scalping/
│       │   ├── ema_scalp.py
│       │   ├── bb_squeeze.py
│       │   └── vwap_reversion.py
│       ├── day_trading/
│       │   ├── macd_ema_trend.py
│       │   ├── sr_breakout.py
│       │   └── rsi_divergence.py
│       └── swing/
│           ├── ema_trend_rider.py
│           ├── fibonacci_rsi.py
│           └── weekly_breakout.py
├── ai/
│   ├── predictor.py              # LSTM/Transformer price prediction
│   ├── rl_agent.py               # Reinforcement learning agent
│   └── signal_scorer.py         # Confidence scoring per signal
├── api/
│   ├── main.py                   # FastAPI entry point + lifespan
│   ├── signal_bus.py             # Singleton signal queue + auto/manual execution
│   ├── runner_loop.py            # Background asyncio loop — runs strategies on schedule
│   ├── routes/
│   │   ├── account.py
│   │   ├── trades.py
│   │   ├── signals.py
│   │   └── config.py
│   └── websocket/
│       └── feed.py               # Live price WebSocket
├── dashboard/                    # Next.js frontend
│   ├── components/
│   │   ├── Chart/                # TradingView chart + overlays
│   │   ├── TradePanel/           # Open positions, confirm/reject
│   │   ├── SignalQueue/          # Pending signals list
│   │   ├── StrategyConfig/       # Per-mode strategy settings
│   │   ├── RiskConfig/           # Risk + capital settings
│   │   ├── Notifications/        # In-app toast + notification center
│   │   └── TradingGuide/         # Built-in reference guide
│   └── pages/
│       ├── index.tsx             # Overview dashboard
│       ├── scalping.tsx
│       ├── day-trading.tsx
│       └── swing.tsx
├── mql5/
│   └── AIBotScalper.mq5          # Expert Advisor for scalping execution
├── data/                         # Backtest run history (JSON per run + _index.jsonl)
├── config/
│   ├── symbols.json              # Symbol list per trading type
│   ├── strategies.json           # Active strategy per mode/symbol
│   ├── risk.json                 # Risk parameters
│   └── app.json                  # Execution mode per trading type (manual/auto)
├── ai/
│   ├── predictor.py              # LSTM price predictor
│   ├── rl_agent.py               # Q-table RL agent (per trading type)
│   ├── param_optimizer.py        # Walk-forward strategy parameter optimizer
│   ├── trade_memory.py           # Closed trade outcome store (JSONL)
│   ├── signal_scorer.py          # Per-signal confidence scorer
│   └── data/
│       ├── trade_memory.jsonl    # Permanent trade outcome log
│       ├── rl_qtable_*.json      # Persisted Q-tables per trading type
│       └── opt_params.json       # Optimized strategy params per symbol
├── engine/
│   ├── backtester.py             # Walk-forward backtest engine (user-facing)
│   ├── mt5_client.py             # MT5 connection wrapper
│   ├── strategies/               # 9 strategy classes (3 per trading type)
│   ├── risk_manager.py           # Position sizing, drawdown circuit breaker
│   ├── order_manager.py          # Order send/modify/close
│   ├── news_filter.py            # Forex Factory news pause logic
│   └── session_filter.py         # Session-aware signal suppression
├── api/
│   ├── main.py                   # FastAPI app, router registration
│   ├── signal_bus.py             # Signal → confidence → RL gate → order → record
│   ├── runner_loop.py            # Background strategy polling loop
│   └── routes/
│       ├── account.py            # Balance, positions, trade history
│       ├── signals.py            # Pending signals, confirm/reject
│       ├── trades.py             # Open/close trades
│       ├── config.py             # Settings read/write
│       ├── risk.py               # Risk parameter overrides
│       ├── ai.py                 # LSTM, RL, optimizer, trade memory endpoints
│       └── backtest.py           # Backtest run + saved history CRUD
├── dashboard/
│   └── app/
│       ├── scalping/             # Scalping chart + trade panel
│       ├── day-trading/          # Day trading chart + trade panel
│       ├── swing/                # Swing chart + trade panel
│       ├── backtest/             # Backtest page (run + history)
│       ├── ml/                   # AI/ML Brain page
│       ├── notifications/        # Notification history
│       ├── settings/             # Settings panel
│       └── guide/                # Trading reference guide
├── docs/                         # This documentation
├── start.bat                     # 1-click launcher: API + dashboard
└── stop.bat                      # Kills all processes
```

---

## Build Phases

| Phase | Deliverable | Status |
|---|---|---|
| 1 | MT5 connection, account info, OHLCV fetch, place/close orders | ✅ Done (`44a1a04`) |
| 2 | Strategy engine + 9 strategies (3 per type) + paper trading | ✅ Done (`1d3d63a`) |
| 3 | FastAPI backend + WebSocket live price feed | ✅ Done (`44a1a04`) |
| 4 | Next.js dashboard — 3 separate trading dashboards + charts | ✅ Done (`c152c0d`) |
| 5 | Execution loop, signal bus, auto/manual mode toggle, browser notifications | ✅ Done (`2e18f82`) |
| 6 | AI price prediction model + signal confidence scoring | ✅ Done (`af1818b`) |
| 7 | Reinforcement learning agent training loop | ✅ Done (`0fbe1bd`) |
| 8 | Risk manager hardening + news filter + drawdown circuit breaker | ✅ Done (`982d8ef`) |
| 9 | Demo ↔ Live account switching + full paper trade sync | ✅ Done (`36e0437`) |
| 10 | `start.bat` / `stop.bat` — 1-click Windows launcher (no Docker) | ✅ Done |
| 11 | Collapsible sidebar, custom MA overlay, sub-chart panel alignment | ✅ Done (`ef23e26`) |
| 12 | AI/ML Brain page — LSTM train, RL status, param optimizer, trade memory | ✅ Done (`0ad92fb`) |
| 13 | Walk-forward backtest engine + backtest page with history, grouped symbol dropdown | ✅ Done (`9ed34cd`) |
| 14 | Backtest → trade memory feed + auto optimizer trigger after ≥30 simulated trades | ✅ Done |

---

## Key Constraints

- `MetaTrader5` Python library **only runs on Windows** — the engine runs locally on Windows; no Docker, no VM required
- MQL5 EA runs **inside the MT5 terminal** — MT5 must be open for scalping execution; Python strategies can run headlessly for day/swing trading
- Paper trading uses the **XM Demo account** — trades appear on XM's servers as real demo trades
- Weekend market gaps and session-open gaps are **real market events** — the chart will show them; they cannot and should not be removed
- Stock CFDs (TSLA, NVDA, etc.) only trade during **exchange hours** — the bot enforces this automatically
- Futures instruments with expiry dates (US100-JUN26, OIL-MAY26, etc.) are **excluded** — Cash instruments only to avoid rollover complexity
