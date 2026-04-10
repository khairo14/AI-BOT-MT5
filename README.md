# EVOTRADE-AI

An AI-powered multi-mode trading bot for XM (MetaTrader 5) with scalping, day trading, and swing trading — each with its own dashboard, strategies, and symbol scope. Supports paper (demo) and live trading with manual confirmation or fully automatic execution.

---

## Documentation

| Doc | Description |
| --- | --- |
| [01 — Architecture](docs/01-architecture.md) | Full system design, tech stack, project structure, build phases |
| [02 — Symbol Scope](docs/02-symbol-scope.md) | Which instruments are traded per mode, why, and the config schema |
| [03 — Strategies](docs/03-strategies.md) | All 9 strategies (3 per mode) with full logic, indicators, entry/exit rules |
| [04 — Risk & Configuration](docs/04-risk-and-configuration.md) | Risk parameters, drawdown protection, position sizing, all config files |
| [05 — Trading Reference Guide](docs/05-trading-reference-guide.md) | In-app guide: trading types, terms, indicators, chart patterns, market sessions |

---

## Quick Summary

| Feature | Detail |
| --- | --- |
| Broker | XM (demo + live MT5 accounts) |
| Trading Modes | Scalping (M1–M5), Day Trading (M15–H1), Swing Trading (H4–D1) |
| Strategies | 9 total — 3 per mode |
| Execution | MQL5 EA for scalping, Python MT5 lib for day/swing |
| Frontend | Next.js dashboard — separate page per trading mode |
| Charts | TradingView Lightweight Charts (live, real-time, no gaps during market hours) |
| AI | LSTM price prediction + signal confidence scoring + RL agent |
| Paper Trading | XM Demo account (all trades recorded on XM servers) |
| Risk | Per-trade % sizing, mandatory SL, drawdown circuit breaker, news filter |

---

## 🚀 Quick Start

### Prerequisites

- **Windows 10/11** (MetaTrader 5 runs on Windows only)
- **Python 3.11+** (tested with 3.11.8)
- **Node.js 18+** (for Next.js dashboard)
- **MetaTrader 5 Terminal** (download from [XM](https://www.xm.com/mt5))
- **XM Account** (demo for paper trading, live for real trading)

### Setup Steps

#### 1️⃣ Install Dependencies

```powershell
# Python backend dependencies
pip install -r requirements.txt

# Optional: ML/AI dependencies (for LSTM prediction)
pip install -r requirements-ml.txt

# Dashboard dependencies
cd dashboard
npm install
cd ..
```

#### 2️⃣ Configure Secrets (Task #6 ✅)

##### Option A: Automated Setup (Recommended)

```powershell
# Run interactive setup script
.\setup_secrets.bat
```

##### Option B: Manual Setup

```powershell
# Copy environment template
Copy-Item .env.example .env

# Edit with your MT5 credentials
notepad .env
```

Fill in your **MT5 credentials** in `.env`:

```bash
# MT5 Demo Account
MT5_DEMO_LOGIN=1301109267
MT5_DEMO_PASSWORD=your_demo_password_here
MT5_DEMO_SERVER=XMGlobal-MT5 6

# MT5 Live Account
MT5_LIVE_LOGIN=420033339
MT5_LIVE_PASSWORD=your_live_password_here
MT5_LIVE_SERVER=XMGlobal-MT5 18
```

**Validate Setup:**

```powershell
python engine\validate_secrets.py
```

#### 3️⃣ Start Backend & Dashboard

```powershell
# Start FastAPI backend (port 8000)
.\start.bat

# In another terminal, start Next.js dashboard (port 3000)
cd dashboard
npm run dev
```

#### 4️⃣ Access Dashboard

Open [http://localhost:3000](http://localhost:3000) and you'll see:

- **Overview** — Active positions, account balance, equity curve
- **Scalping** — M1-M5 signals, 5 symbols (EURUSD, GBPUSD, etc.)
- **Day Trading** — M15-H1 signals, 4 symbols
- **Swing** — H4-D1 signals, 11 symbols
- **Scanner** — Search 850+ XM symbols with AI filters
- **Profitability** — Real-time win rate, profit factor, validation status

---

## 🔒 Security Notes

- ✅ **Passwords are never committed to git** (`.env` excluded via `.gitignore`)
- ✅ **Environment variables** used for all sensitive data
- ✅ **Login numbers** in `config/app.json` are non-sensitive (account IDs only)
- ⚠️ **Production deployment:** Consider AWS Secrets Manager for key rotation (see [Task #6 docs](docs/15-task6-secrets-management.md))

---

## 📊 System Status

| Component | Status | Notes |
| --- | --- | --- |
| Market Scanner | ✅ Live | 850+ symbols, AI-powered filtering |
| Profitability Validation | ⚙️ Collecting Data | 64 trades, 21.88% WR (target: 50%) |
| Paper Trading | ✅ Working | All trades logged to XM demo servers |
| Live Trading | ⚠️ Not Recommended | Wait for profitability validation (50% WR, 1.5 PF) |
| Secrets Management | ✅ Complete | Environment variables, password rotation ready |

---
