---
name: debug-trade-mode
description: 'Debug trade mode issues in AI-BOT-MT5. Trade modes are scalping, day_trading, and swing. Account mode is paper or live. Use when: signals not executing, wrong trade mode or account mode, account mode switch failures, execution_mode auto/manual not working, circuit breakers blocking a trade mode, RL agent loading wrong Q-table, EA fast-path not activating, paper engine ignoring orders, or lot size calculated as zero. Covers full lifecycle from config → signal_bus → risk_manager → order_manager.'
argument-hint: 'describe the symptom (e.g. "scalping signals not auto-executing", "account mode switch fails", "paper orders not placed")'
---

# Debug Trade Mode

Three axes govern execution in AI-BOT-MT5:

| Axis | Values | Config |
|---|---|---|
| **Trade mode** | `scalping` / `day_trading` / `swing` | Per-signal `trading_type` field; scan intervals in `config/scanner.json` |
| **Account mode** | `paper` / `live` | `config/account_mode.json` → `{"mode": "paper"}` |
| **Execution mode** | `auto` / `manual` (per trade mode) | `config/app.json` → `execution_mode.{scalping\|day_trading\|swing}` |
| **EA fast-path** | enabled / disabled | `config/app.json` → `ea_enabled` + EA bridge active (scalping only) |

---

## Step 1 — Identify the Symptom

| Symptom | Go to section |
|---|---|
| Signal generated but never executed | § Execution Mode (auto / manual) |
| Signal not even generated | § Risk Manager Gates |
| Paper orders not placed / silently dropped | § Paper Engine Guard |
| Account mode switch hangs or partially applies | § Account Mode Switch Lifecycle |
| EA fast-path not used for scalping | § EA Bridge Fast-Path |
| RL agent behaving wrong after account mode switch | § RL Agent Q-Tables |
| Lot size is 0 / order rejected | § Lot Size Guard |
| Duplicate signal blocked | § Signal Dedup |

---

## Step 2 — Verify Account Mode State

**Account mode** (`paper` / `live`) controls which MT5 credentials are used and whether `PaperTradeEngine` is active.

**Read the effective account mode** (priority order applied by `load_mode()`):

1. `config/account_mode.json` — `{"mode": "paper"}` or `{"mode": "live"}`
2. `TRADING_MODE` env var (overrides file if set)
3. Falls back to `"paper"` if both missing

**Check at runtime:**

```python
from engine.account_store import current_mode
print(current_mode())          # "paper" or "live"
```

**Check MT5Client is in sync:**

```python
from engine.mt5_client import MT5Client
client = MT5Client()
print(client.trading_mode)    # Must match account_store
```

**Mismatch signal:** `client.trading_mode` differs from `account_store.current_mode()` → `switch_mode()` was interrupted before `save_mode()` ran. Fix: call `POST /account/switch-mode` again.

---

## Execution Mode (auto / manual)

File: [api/signal_bus.py](../../../api/signal_bus.py)
Config: `config/app.json` → `execution_mode.{scalping|day_trading|swing}`

**Execution mode is per trade mode** — you can have scalping on `auto` while swing is on `manual`.

**Resolution path inside `add_signal()`:**

1. `_get_exec_mode(trading_type)` reads `app.json["execution_mode"][trading_type]`, defaults `"manual"`
2. `"auto"` → immediately runs RISK-5 pre-check → dispatches `_execute_async()`
3. `"manual"` → signal queued as `pending` with expiry; waits for dashboard approval

**Debug checklist:**

- [ ] Confirm `app.json["execution_mode"]` matches expectation for the affected **trade mode** (scalping / day_trading / swing)
- [ ] If `"auto"` but not executing: check confidence floor next (§ below)
- [ ] If `"manual"` but pending signals disappear: check signal expiry (`_DEFAULT_EXPIRY` per timeframe — scalping = 10 s, day_trading = 90 s, swing = 600 s)
- [ ] If signal appears in dashboard but "Execute" does nothing: check RISK-5 synchronous gate via `is_trading_allowed(trading_type)`

**Confidence floor (auto execution only, when `confidence_filter_enabled: true`):**

| Trade mode | Minimum confidence |
|---|---|
| scalping | 0.60 |
| day_trading | 0.50 |
| swing | 0.45 |

Signals below threshold are **silently dropped** — not queued, not broadcast on WS. Enable debug logging and watch for `"confidence below floor"` log lines.

---

## Risk Manager Gates

File: [engine/risk_manager.py](../../../engine/risk_manager.py)
State: [data/risk_state.json](../../../data/risk_state.json)

The risk manager tracks circuit breakers **per trade mode** (scalping / day_trading / swing) independently, plus global halts that stop all trade modes.

**`is_trading_allowed(trading_type)` gate order:**

1. `_daily_halted` → global halt, blocks all three trade modes
2. `_weekly_halted` → global halt, blocks all three trade modes
3. `_paused_modes[trading_type]` → per-trade-mode pause; check if `now() >= pause_expiry`

**`check_concurrent_limit(trading_type, ...)` additional guards:**

- Position count per trade mode (MT5 comment prefix: `scalp|`, `day|`, `swing|`)
- `_mode_switch_ts` 1-second settling hold after any account mode `switch_mode()` call

**Debug checklist:**

- [ ] Read `data/risk_state.json` — check `_daily_halted`, `_weekly_halted`, `_paused_modes` (keyed by trade mode name)
- [ ] Check `_consecutive_losses` per trade mode against `max_consecutive_losses` in `config/risk.json`
- [ ] If a specific trade mode is paused: check `_paused_modes["scalping"|"day_trading"|"swing"]` for expiry timestamp
- [ ] If all trade modes halted unexpectedly: `reset_for_mode_switch()` clears all circuit breakers — also called automatically by `POST /account/switch-mode`
- [ ] 1-second hold blocking new orders right after account mode switch: wait or re-run after 2 seconds

**Manual reset (use with caution — paper account mode only):**

```python
from engine.risk_manager import RiskManager
rm = RiskManager()
rm.reset_for_mode_switch()     # clears all circuit breakers + per-trade-mode pauses
```

---

## Paper Engine Guard

File: [engine/paper_trade.py](../../../engine/paper_trade.py) — `_verify_paper_mode()`

Hard guard at top of both `scan_and_signal()` and `place_paper_order()`:

```python
return self.client.trading_mode == "paper"
```

Returns `[]` / `None` immediately if account mode is not `"paper"`.

**Symptom:** paper orders silently ignored.
**Cause:** `client.trading_mode` is `"live"` while code path reached `PaperTradeEngine`.
**Fix:** Confirm account mode is `"paper"` (see § Step 2).

**Paper positions ledger** (`_positions: dict[int, PaperPosition]`) is **in-memory only**.

- After process restart, it is rebuilt from `sync_positions()` reconciling with real MT5 demo account
- If positions appear open in MT5 demo but not in bot: trigger `paper_engine.sync_positions()` manually or restart the API

---

## Account Mode Switch Lifecycle

Endpoint: `POST /account/switch-mode`  
File: [api/routes/account.py](../../../api/routes/account.py)

This switches the **account mode** between `paper` and `live`. It does NOT change which trade modes (scalping / day_trading / swing) are active.

**Full sequence:**

```
1. Validate account mode ∈ {"paper", "live"}
2. No-op if already that account mode
3. Guard: open positions block switch (pass force=true to bypass)
4. pause_runner()                         ← runner_loop stops all trade mode scanning
5. client.switch_mode(mode)               ← MT5 disconnect → re-login to new account → save_mode()
6. resume_runner()                        ← runner_loop resumes all trade mode scanning
7. risk_manager.reset_for_mode_switch()   ← clears ALL per-trade-mode circuit breakers
8. rl_manager.switch_mode(mode)           ← reloads Q-tables for new account mode
```

**Failure scenarios:**

| Failure point | Symptom | Fix |
|---|---|---|
| Step 3 — open positions | 400 response, switch blocked | Close positions or pass `force=true` |
| Step 5 fails (MT5 credentials wrong) | `client.trading_mode` updated in memory, `save_mode()` NOT persisted | Re-check `MT5_DEMO_*` / `MT5_LIVE_*` env vars; retry switch |
| Step 7 skipped (exception in step 5/6) | Old account mode's circuit breakers still active for all trade modes | Call `reset_for_mode_switch()` manually |
| Step 8 skipped | RL agent using wrong account mode Q-tables | Call `rl_manager.switch_mode(mode)` manually |
| `runner_loop._paused` stuck `True` | All trade mode runners never resume scanning | Call `resume_runner()` in `api/runner_loop.py` |

**Required env vars by account mode:**

| Account mode | Env vars needed |
|---|---|
| paper | `MT5_DEMO_LOGIN`, `MT5_DEMO_PASSWORD`, `MT5_DEMO_SERVER` |
| live | `MT5_LIVE_LOGIN`, `MT5_LIVE_PASSWORD`, `MT5_LIVE_SERVER` |

---

## EA Bridge Fast-Path

Files: [engine/ea_bridge.py](../../../engine/ea_bridge.py), [api/signal_bus.py](../../../api/signal_bus.py)

EA fast-path activates **only when all three conditions are true:**

1. `app.json["ea_enabled"] == true`
2. `ea_bridge.is_active()` returns `True` (socket open, EA process responding)
3. `trading_mode == "scalping"`

**Debug checklist:**

- [ ] Verify `config/app.json` → `"ea_enabled": true`
- [ ] Check EA is connected: `ea_bridge.is_active()` / look for connection log line
- [ ] If EA inactive: bot falls back to Python `OrderManager` path automatically (no error)
- [ ] MQL5 EA source: [mql5/AIBotScalper.mq5](../../../mql5/AIBotScalper.mq5)
- [ ] Magic number must match: `app.json["bot_magic"]` == `INPUT_MAGIC` in EA

---

## RL Agent Q-Tables

File: [ai/rl_agent.py](../../../ai/rl_agent.py)
Data: [ai/data/](../../../ai/data/) — `rl_qtable_{trade_mode}_{account_mode}.json`

Q-tables are scoped by **both** trade mode (scalping / day_trading / swing) and account mode (paper / live) — 6 files total.

**Expected files:**

| Account mode | Files (one per trade mode) |
|---|---|
| paper | `rl_qtable_scalping_paper.json`, `rl_qtable_day_trading_paper.json`, `rl_qtable_swing_paper.json` |
| live | `rl_qtable_scalping_live.json`, `rl_qtable_day_trading_live.json`, `rl_qtable_swing_live.json` |

**Debug checklist:**

- [ ] If file missing: that trade mode's agent starts with empty Q-table (no error — learns from scratch)
- [ ] After account mode switch: `rl_manager.switch_mode(account_mode)` must be called — rebuilds all 3 trade mode agents for the new account mode
- [ ] If RL `risk_factor` seems wrong for a specific trade mode (lot sizes too small/large): dump that Q-table and inspect state-action values

---

## Signal Dedup

File: [api/signal_bus.py](../../../api/signal_bus.py) — `_pending_keys`

Dedup key: `(symbol, strategy, direction, trading_type)` where `trading_type` is the trade mode (`scalping` / `day_trading` / `swing`).

**Symptom:** signal generated by strategy but silently dropped on second run.
**Cause:** same `(symbol, strategy, direction, trade_mode)` tuple already in `_pending_keys`.
**Fix:** Previous signal must be executed, rejected, or expired before a new one is accepted.
**Note:** Signals from different trade modes for the same symbol are tracked independently — a scalping signal and a swing signal for the same symbol are separate dedup keys.

---

## Lot Size Guard

File: [api/signal_bus.py](../../../api/signal_bus.py)

Guard: `if _lot <= 0: drop signal`

**Root cause:** SL distance is 0 (entry price == SL price) → lot calculation divides by zero or returns 0.

**Debug checklist:**

- [ ] Check strategy SL logic for the affected symbol
- [ ] Confirm symbol `tick_value` / `tick_size` are returned from MT5 (can fail if symbol not in Market Watch)
- [ ] Add symbol to MT5 Market Watch if tick info is `None`

---

## Quick Diagnostic Sequence

For any "trade not executing" report, run this order:

```
1. Which trade mode?                   → scalping / day_trading / swing
2. current_mode()                      → confirm account mode: paper or live
3. app.json execution_mode[trade_mode] → confirm auto/manual for that trade mode
4. risk_state.json                     → check _daily_halted, _weekly_halted, _paused_modes[trade_mode]
5. signal_bus pending_signals          → is signal queued or expired?
6. signal_bus logs for confidence drop → silent drop below floor?
7. paper_trade._verify_paper_mode()    → paper account guard blocking? (account mode must be "paper")
8. lot size > 0?                       → SL distance check
9. EA active?                          → only relevant for scalping trade mode
```
