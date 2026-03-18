# AI-BOT-MT5 — System Architecture

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

- **Price Prediction**: LSTM or Transformer model trained per symbol on OHLCV data
- **Signal Confidence Scoring**: AI assigns a 0–100 confidence score to each strategy signal before execution; low-confidence signals can be filtered or flagged
- **Reinforcement Learning Agent**: learns from trade outcomes (P&L, drawdown, win rate) and improves parameter selection over time
- Training data stored in PostgreSQL
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

- REST + WebSocket server
- Bridges the frontend dashboard with the trading engine
- JWT-authenticated endpoints
- Key endpoints:
  - `GET /account` — balance, equity, margin
  - `GET /positions` — open trades
  - `GET /history` — closed trade history
  - `GET /signals` — pending signals queue
  - `POST /trade/confirm` — manual trade confirmation
  - `POST /trade/reject` — reject a pending signal
  - `WebSocket /feed` — live price tick stream per symbol
  - `PATCH /config` — update strategy/risk configuration

### Layer 7 — Web Dashboard (Next.js / TypeScript)

- 4 main sections: **Scalping**, **Day Trading**, **Swing Trading**, **Overview**
- Each trading dashboard is fully separate with its own chart, trade panel, strategy config, and active positions
- TradingView Lightweight Charts — live price feed, no missing candles during market hours, all standard indicators built in
- Open trade overlays on chart (entry price line, SL line, TP line)
- Manual confirmation mode: signal popup → user clicks Approve or Reject → order fires
- Auto mode: order fires immediately on signal
- In-app notification system: browser toasts for signals, fills, SL/TP hits, drawdown alerts
- Built-in trading reference guide (see [05-trading-reference-guide.md](./05-trading-reference-guide.md))
- Account switcher: Demo ↔ Live

---

## Tech Stack

| Component | Technology |
|---|---|
| MT5 Connection | `MetaTrader5` Python library |
| Scalping Order Execution | MQL5 Expert Advisor (inside MT5 terminal) |
| Data & Indicators | `pandas`, `pandas-ta`, `numpy` |
| Strategy Engine | Python classes (modular) |
| AI / Price Prediction | `PyTorch` (LSTM / Transformer) |
| Reinforcement Learning | `stable-baselines3` or custom RL loop |
| Signal Confidence Scoring | `scikit-learn` classifier |
| Backend API | `FastAPI` + `WebSocket` |
| Task Queue | `asyncio` background tasks (built into FastAPI) |
| Frontend | `Next.js` + TypeScript |
| Charts | TradingView Lightweight Charts |
| Real-time Feed | WebSocket (MT5 ticks → FastAPI → UI) |
| Database | `PostgreSQL` |
| Auth | JWT |
| Deployment | `start.ps1` / `stop.ps1` — Windows-native launcher |

---

## Project Directory Structure

```
AI-BOT-MT5/
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
├── data/                         # Historical OHLCV, ML training sets
├── db/                           # PostgreSQL models + Alembic migrations
├── config/
│   ├── symbols.json              # Symbol list per trading type
│   ├── strategies.json           # Active strategy per mode/symbol
│   ├── risk.json                 # Risk parameters
│   └── app.json                  # Execution mode per trading type (manual/auto)
├── start.ps1                     # 1-click launcher: API + dashboard
└── stop.ps1                      # Kills all processes
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
| 6 | AI price prediction model + signal confidence scoring | Not Started |
| 7 | Reinforcement learning agent training loop | Not Started |
| 8 | Risk manager hardening + news filter + drawdown circuit breaker | Not Started |
| 9 | Demo ↔ Live account switching + full paper trade sync | Not Started |
| 10 | `start.ps1` / `stop.ps1` — 1-click Windows launcher (no Docker) | Not Started |

---

## Key Constraints

- `MetaTrader5` Python library **only runs on Windows** — the engine runs locally on Windows; no Docker, no VM required
- MQL5 EA runs **inside the MT5 terminal** — MT5 must be open for scalping execution; Python strategies can run headlessly for day/swing trading
- Paper trading uses the **XM Demo account** — trades appear on XM's servers as real demo trades
- Weekend market gaps and session-open gaps are **real market events** — the chart will show them; they cannot and should not be removed
- Stock CFDs (TSLA, NVDA, etc.) only trade during **exchange hours** — the bot enforces this automatically
- Futures instruments with expiry dates (US100-JUN26, OIL-MAY26, etc.) are **excluded** — Cash instruments only to avoid rollover complexity
