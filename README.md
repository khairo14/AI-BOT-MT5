# AI-BOT-MT5

An AI-powered automated trading bot for the **XM broker** using the **MetaTrader 5 (MT5)** platform.

---

## Features

- **AI/ML Signal Generation** – Trains a Random Forest classifier on technical indicators to generate BUY / SELL / HOLD signals with configurable confidence thresholds.
- **Technical Indicators** – RSI, MACD, Bollinger Bands, EMA, ATR, Stochastic Oscillator (all computed in-house via `pandas`/`numpy`).
- **Risk Management** – Fixed-fractional position sizing, configurable stop-loss / take-profit, max simultaneous trades, and daily loss limits.
- **Automatic Retraining** – The model retrains itself on a configurable schedule using the latest market data.
- **Model Persistence** – Trained models are saved to disk and reloaded on restart.
- **Graceful Shutdown** – Handles `SIGINT`/`SIGTERM`, closes all open positions before exiting.
- **Environment Variable Overrides** – MT5 credentials can be supplied via `.env` or OS environment variables (never hardcode secrets).

---

## Project Structure

```
AI-BOT-MT5/
├── config/
│   └── config.yaml          # Main configuration file
├── src/
│   ├── __init__.py
│   ├── bot.py               # Main entry point / trading loop
│   ├── config.py            # Configuration loader
│   ├── data_fetcher.py      # MT5 OHLCV data retrieval
│   ├── indicators.py        # Technical indicator calculations
│   ├── logger.py            # Logging setup
│   ├── mt5_connector.py     # MT5 connection manager
│   ├── order_manager.py     # Order placement & position management
│   ├── risk_manager.py      # Position sizing & risk controls
│   └── strategy.py          # AI/ML trading strategy
├── tests/
│   ├── test_config.py
│   ├── test_indicators.py
│   ├── test_risk_manager.py
│   └── test_strategy.py
├── .env.example             # Template for credentials
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Requirements

- **Python** ≥ 3.10
- **MetaTrader 5** terminal installed on Windows (the `MetaTrader5` Python package is Windows-only)
- An active [XM](https://www.xm.com/) trading account (demo or live)

---

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/khairo14/AI-BOT-MT5.git
cd AI-BOT-MT5
```

### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # macOS / Linux
pip install -r requirements.txt
```

### 3. Configure credentials

Copy `.env.example` to `.env` and fill in your XM account details:

```
MT5_LOGIN=123456789
MT5_PASSWORD=YourPassword
MT5_SERVER=XMGlobal-MT5
```

> **Never commit your `.env` file** – it is listed in `.gitignore`.

### 4. Edit `config/config.yaml`

Adjust the trading symbol, timeframe, risk parameters, and strategy settings to match your preferences.

---

## Running the Bot

```bash
python -m src.bot
# or specify a custom config path
python -m src.bot --config path/to/config.yaml
```

The bot will:
1. Connect to MT5 using your credentials.
2. Load (or train) the AI model.
3. Enter the main loop: fetch candles → compute indicators → predict signal → manage orders.
4. On shutdown (`Ctrl+C`), close all open positions and disconnect.

---

## Running Tests

```bash
python -m pytest tests/ -v
```

All tests run without a live MT5 connection (MT5 calls are stubbed out).

---

## Configuration Reference

| Section | Key | Description |
|---------|-----|-------------|
| `mt5` | `login` | XM account number |
| `mt5` | `server` | Broker server (e.g. `XMGlobal-MT5`) |
| `trading` | `symbol` | Trading instrument (e.g. `EURUSD`) |
| `trading` | `timeframe` | Chart timeframe (`M1`–`MN1`) |
| `risk` | `risk_per_trade` | Fraction of balance to risk per trade (e.g. `0.01` = 1%) |
| `risk` | `max_trades` | Maximum simultaneous open trades |
| `risk` | `stop_loss_pips` | Stop-loss distance in pips |
| `risk` | `take_profit_pips` | Take-profit distance in pips |
| `risk` | `max_daily_loss` | Daily loss limit as fraction of balance |
| `strategy` | `prediction_threshold` | Minimum model confidence to act on a signal |
| `strategy` | `retrain_interval` | Hours between model retraining sessions |

---

## Disclaimer

This software is for **educational and research purposes only**. Trading foreign exchange and CFDs carries a high level of risk. Past performance is not indicative of future results. Always test on a **demo account** before using real funds.
