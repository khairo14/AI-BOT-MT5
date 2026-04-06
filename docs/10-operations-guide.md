# EVOTRADE-AI — Operations Guide

**Last Updated:** April 2026

---

## First-Time Setup

### Prerequisites

1. MetaTrader 5 terminal installed and logged into XM Demo account
2. Python 3.11+ with virtual environment activated
3. Dependencies installed: `pip install -r requirements.txt` and `pip install -r requirements-ml.txt`
4. `.env` file populated with MT5 credentials for both paper and live accounts
5. `config/symbols.json`, `config/strategies.json`, `config/risk.json`, `config/app.json` present

### AI/ML Initialization (run once before going live)

Run in this order:

```bash
# Step 1 — Run in parallel (two terminals simultaneously)
# Terminal 1:
python ai/run_retrain.py              # trains day_trading + swing LSTMs (~1–2 hours)
python ai/run_retrain_scalping.py     # trains scalping LSTMs (run after retrain.py finishes)

# Terminal 2 (simultaneously with Terminal 1):
python ai/run_optimizer.py            # optimizes all 9 strategies × all symbols (~4–8 hours)

# Step 2 — Only after BOTH complete
python ai/run_rl_bootstrap.py         # seeds Q-tables from backtest outcomes with LSTM scores
```

**Why this order matters:**
- Retrain and optimizer are independent — they write to different files, have zero dependency on each other, and can safely run simultaneously
- Bootstrap REQUIRES both to complete first:
  - Without LSTM: all backtest `conf_score = 0.55` uniform → RL cannot learn confidence-outcome relationships
  - Without optimizer: strategies use default params → backtest win rate may not represent live behaviour

---

## Daily Operations

### Starting the Bot

```bash
start.bat        # Windows: starts FastAPI backend + Next.js dashboard
```

Or manually:
```bash
# Terminal 1:
uvicorn api.main:app --reload --port 8000

# Terminal 2:
cd dashboard && npm run dev
```

Open dashboard: `http://localhost:3000`

### Stopping the Bot

```bash
stop.bat         # Kills all processes; Q-tables force-saved before shutdown
```

---

## Weekly Maintenance

### Recommended Weekly Cycle

```bash
# 1. Stop the bot
stop.bat

# 2. Re-run retrain (captures last week's market patterns)
python ai/run_retrain.py
python ai/run_retrain_scalping.py

# 3. Re-run optimizer (skips already-done 2yr jobs, runs new ones)
python ai/run_optimizer.py

# 4. Re-bootstrap (after both complete)
python ai/run_rl_bootstrap.py

# 5. Restart the bot
start.bat
```

> The optimizer uses `_DONE_BARS_THRESHOLD = 30,000` — jobs already optimized with ≥30k bars are skipped. Only new symbols or strategies that haven't been optimized yet (or whose results were cleared) will run. A full re-run is fast if most jobs are cached.

---

## Account Reset / Fresh Start

When switching to a new demo account or clearing all historical data:

### Files to wipe (in order, bot stopped)

| File | Action | Reason |
|---|---|---|
| `data/trade_journal.jsonl` | **Wipe** (after backup) | Fresh account = fresh history |
| `ai/data/trade_memory.jsonl` | **Wipe** | Outcome scorer memory biased by old data |
| `ai/data/rl_qtable_scalping_paper.json` | **Delete** | RL unlearns old loss history |
| `ai/data/rl_qtable_day_trading_paper.json` | **Delete** | Same reason |
| `ai/data/rl_qtable_swing_paper.json` | **Delete** | Same reason |
| `data/risk_state.json` | **Reset** | New account must not inherit old CB state |

### Files to update

| File | What to change |
|---|---|
| `config/app.json` | Update `accounts.paper.login` with new demo account number |

### Files to leave alone

| File | Reason |
|---|---|
| `ai/models/` | LSTM weights are symbol/strategy specific, not account-specific |
| `config/optimized_params.json` | Optimizer results are symbol/strategy specific |
| `data/regime_state.json` | Reads from live market — not account-specific |
| `ai/data/optimizer_status.json` | Not account-linked |

### After reset

Re-run the full bootstrap sequence (Step 1 + Step 2 from First-Time Setup).

---

## Scalping RL Reset (after strategy config changes)

If scalping parameters or RR targets are changed significantly, the RL Q-table may have learned from a broken configuration and should be reset:

```bash
python ai/reset_scalping_rl.py --mode paper
# Options:
# --mode live         Reset live Q-table
# --both              Reset both paper and live
# --keep-qtable       Reset conf/risk only, keep Q-values
```

This resets `conf_thresh → 0.55` and `risk_factor → 1.00` while preserving `n_updates` (so epsilon decay level is maintained). The bot will relearn from the corrected strategy geometry.

---

## Model Rollback

If a freshly retrained LSTM model performs worse in live trading:

```bash
# Via API:
POST /ai/models/rollback/{symbol}/{trading_type}
# e.g. POST /ai/models/rollback/EURUSD/scalping
```

This copies the previous version from `ai/models/versions/{key}/` over the current model and immediately reloads it in memory. Up to 3 versions are kept per model key.

---

## Config Validation

After any manual edits to config files, verify all files are valid JSON:

```bash
GET /config/validate
```

Returns:
```json
{
  "all_ok": true,
  "files": {
    "app.json": "ok",
    "risk.json": "ok",
    "symbols.json": "ok",
    "strategies.json": "ok",
    "scanner.json": "ok",
    "account_mode.json": "ok"
  }
}
```

---

## Execution Mode Switching

Each trading type can run in `manual` or `auto` mode independently:

```bash
# Via API:
PATCH /config/execution-mode/scalping?mode=auto
PATCH /config/execution-mode/day_trading?mode=manual
PATCH /config/execution-mode/swing?mode=auto
```

Or from the dashboard Settings panel.

- **Manual:** signal appears in the queue as a card — you click Approve or Reject
- **Auto:** signal is executed immediately (subject to all confidence and RL gates)

---

## Scanner Configuration

The scanner controls which symbols are actively scanned per mode. Edit from the dashboard Settings → Scanner panel, or via `PATCH /config/scanner`:

```json
{
  "scalping": {
    "enabled": true,
    "symbols": ["EURUSD", "GBPUSD", "USDJPY"]
  },
  "day_trading": {
    "enabled": true,
    "symbols": ["EURUSD", "GOLD", "US100Cash"]
  },
  "swing": {
    "enabled": false
  }
}
```

Limits: scalping max 7 symbols, day_trading max 15, swing max 18. Scanner changes take effect within 5 seconds (TTL-cached config hot-reload).

---

## Monitoring

### Log file

`logs/api.log` — rotating, last 3 files, 10 MB each. Tail from the dashboard via `GET /logs/tail?n=200`.

### Key log patterns to watch

| Log pattern | Meaning |
|---|---|
| `Bar-close guard: no new bar` | Normal — suppressed redundant scan |
| `Regime gate blocked {strategy}/{symbol}` | Strategy suppressed by regime — normal |
| `Correlation guard blocked` | Duplicate correlated exposure — normal |
| `conf-drop ... not queued` | Signal below RL threshold — monitor if frequent |
| `CIRCUIT BREAKER: Daily drawdown` | Trading halted — check positions |
| `Optimizer: could not load params file` | File corrupted — auto-recovery attempted |
| `LSTM trained: ... val accuracy` | Retrain completed — check accuracy |
| `NewsFilter: failed to fetch` | Network issue — using cached data |
| `MT5 reconnected successfully` | Watchdog recovered connection |

### WebSocket performance alerts (dashboard notifications)

| Alert | Action |
|---|---|
| Win rate < 40% for a mode | Check regime, reduce risk, consider pausing |
| RL risk_factor < 0.60 | RL de-risking — monitor for recovery |
| LSTM degraded symbols | Trigger manual retrain for those symbols |
| Win rate drift: high severity | Consider pausing mode; re-run optimizer |
| Anchor accuracy drop > 8 pts | Rollback LSTM model or force retrain |
| 3+ consecutive losses on symbol | Consider disabling that symbol temporarily |

---

## API Quick Reference

| Category | Endpoint | Method |
|---|---|---|
| Health | `/health` | GET |
| Account | `/account/` | GET |
| Mode switch | `/account/switch-mode` | POST |
| Positions | `/trades/positions` | GET |
| Journal | `/trades/journal` | GET |
| Signal queue | `/signals/` | GET |
| Approve signal | `/signals/{id}/approve` | POST |
| Place manual order | `/trades/place` | POST |
| LSTM status | `/ai/status` | GET |
| Retrain symbol | `/ai/train/{symbol}` | POST |
| Retrain all | `/ai/train/all` | POST |
| RL status | `/ai/rl/status` | GET |
| RL reset | `/ai/rl/reset/{type}` | POST |
| Optimizer status | `/ai/optimizer/status` | GET |
| Run optimizer | `/ai/optimizer/run/{strategy}/{symbol}` | POST |
| Trade memory stats | `/ai/memory/stats` | GET |
| Drift detection | `/ai/memory/drift` | GET |
| Analytics | `/analytics/performance` | GET |
| Regime status | `/analytics/regime/status` | GET |
| Run backtest | `/backtest/run` | POST |
| Backtest history | `/backtest/history` | GET |
| Config validate | `/config/validate` | GET |
| Update app config | `/config/app` | PATCH |
| Update risk config | `/config/risk` | PATCH |
| Risk status | `/risk/status` | GET |
| Reset drawdown | `/risk/reset-drawdown` | POST |
| News status | `/risk/news` | GET |
| Session status | `/risk/sessions` | GET |
| Log tail | `/logs/tail` | GET |
| WebSocket feed | `ws://localhost:8000/ws/feed?symbols=EURUSD,GOLD` | WS |
