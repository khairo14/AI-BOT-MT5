# AI-BOT-MT5

An AI-powered multi-mode trading bot for XM (MetaTrader 5) with scalping, day trading, and swing trading — each with its own dashboard, strategies, and symbol scope. Supports paper (demo) and live trading with manual confirmation or fully automatic execution.

---

## Documentation

| Doc | Description |
|---|---|
| [01 — Architecture](docs/01-architecture.md) | Full system design, tech stack, project structure, build phases |
| [02 — Symbol Scope](docs/02-symbol-scope.md) | Which instruments are traded per mode, why, and the config schema |
| [03 — Strategies](docs/03-strategies.md) | All 9 strategies (3 per mode) with full logic, indicators, entry/exit rules |
| [04 — Risk & Configuration](docs/04-risk-and-configuration.md) | Risk parameters, drawdown protection, position sizing, all config files |
| [05 — Trading Reference Guide](docs/05-trading-reference-guide.md) | In-app guide: trading types, terms, indicators, chart patterns, market sessions |

---

## Quick Summary

| Feature | Detail |
|---|---|
| Broker | XM (demo + live MT5 accounts) |
| Trading Modes | Scalping (M1–M5), Day Trading (M15–H1), Swing Trading (H4–D1) |
| Strategies | 9 total — 3 per mode |
| Execution | MQL5 EA for scalping, Python MT5 lib for day/swing |
| Frontend | Next.js dashboard — separate page per trading mode |
| Charts | TradingView Lightweight Charts (live, real-time, no gaps during market hours) |
| AI | LSTM price prediction + signal confidence scoring + RL agent |
| Paper Trading | XM Demo account (all trades recorded on XM servers) |
| Risk | Per-trade % sizing, mandatory SL, drawdown circuit breaker, news filter |