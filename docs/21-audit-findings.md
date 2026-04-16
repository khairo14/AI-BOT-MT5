# AI-BOT-MT5 — Audit Findings

**Last Updated:** April 16, 2026  
**Audit Scope:** Full codebase review — engine, API, AI layer, infrastructure  
**Severity Scale:** 🔴 Critical · 🟠 High · 🟡 Medium · 🟢 Low

---

## Summary

| ID | Area | Severity | Status |
|----|------|----------|--------|
| NEW-1 | RiskManager balance seed on fresh start | 🟡 Medium | ✅ Fixed |
| NEW-3 | partial_close volume rounding order | 🟡 Medium | ✅ Fixed |
| NEW-4 | PaperTradeEngine history unbounded | 🟢 Low | ✅ Fixed |
| NEW-5 | Signal poll timeout (swing 7-day hard cap) | 🟡 Medium | ✅ Fixed |
| NEW-6 | SignalBus fire-and-forget task leak | 🟡 Medium | ✅ Fixed |
| NEW-7 | Runner-loop task accumulation | 🟡 Medium | ✅ Fixed |
| NEW-8 | TradeMemory._load() corrupt line drops rest | 🟡 Medium | ✅ Fixed |
| NEW-9 | RL Q-table not saved at shutdown | 🟡 Medium | ✅ Fixed |
| NEW-10 | news_filter._SYMBOL_CURRENCIES incomplete | 🟢 Low | ✅ Fixed |
| INFRA-1 | No CI pipeline | 🟢 Low | ✅ Fixed (see `.github/workflows/ci.yml`) |
| INFRA-2 | Audit findings not documented | 🟢 Low | ✅ This document |

All medium/low findings from the April 2026 audit are resolved.

---

## Resolved Findings

### NEW-1 — RiskManager: `_day_start_balance` not seeded on fresh start  
**Severity:** 🟡 Medium  
**File:** `engine/risk_manager.py`, `api/main.py`

**Problem:**  
On a fresh install with no `data/risk_state.json`, `_day_start_balance` and
`_week_start_balance` remained `None` until the first trade closed, creating a
window where the daily drawdown circuit breaker could not fire. If the account
was already in intraday loss before the bot started, the drawdown baseline would
silently reset to the post-loss balance.

**Fix:**  
`api/main.py` lifespan (GAP-1 block) calls `risk_manager.update_balance(balance)`
immediately after MT5 connects, passing the live account balance. This seeds both
start-balance fields before any trade executes.

```python
# api/main.py — lifespan startup
_acct_seed = await asyncio.to_thread(mt5_client.get_account_info)
if _acct_seed and _acct_seed.get("balance"):
    _risk_manager.update_balance(float(_acct_seed["balance"]))
```

**Status:** ✅ Fixed

---

### NEW-3 — `partial_close`: volume_min clamp applied before floor-round  
**Severity:** 🟡 Medium  
**File:** `engine/order_manager.py` — `partial_close()`

**Problem:**  
The minimum-lot clamp was applied before the floor-round, so a tiny position
(e.g. 0.01 lot with volume_min = 0.02) could have its close volume rounded up
above the requested fraction.

**Fix:**  
- Floor-round now uses `math.floor` instead of `round` (comment `# BUG-1`)
- Guard A (`min(close_volume, pos.volume)`) ensures close volume never exceeds
  the remaining position size
- Guard B bails out if close volume is zero after rounding

**Status:** ✅ Fixed

---

### NEW-4 — `PaperTradeEngine._history` unbounded growth  
**Severity:** 🟢 Low  
**File:** `engine/paper_trade.py`

**Problem:**  
The closed-position history list was not capped, risking memory growth over
long-running sessions.

**Fix:**  
`_HISTORY_MAX = 1_000` constant added. After every `_history.append(pos)` the
list is trimmed: `self._history = self._history[-_HISTORY_MAX:]`.

**Status:** ✅ Fixed

---

### NEW-5 — Signal poll timeout: swing trades expired after 7 days  
**Severity:** 🟡 Medium  
**File:** `api/signal_bus.py` — `_poll_outcome()`

**Problem:**  
A single hard-coded 7-day poll limit was applied to all trading types. Swing
trades can legitimately stay open for 30–45 days, so they were being expired
too early.

**Fix:**  
`_MAX_POLLS_BY_TYPE` dict provides per-mode timeouts:
```python
_MAX_POLLS_BY_TYPE = {
    "scalping":    2  * 24 * 120,   # 2 days
    "day_trading": 14 * 24 * 120,   # 14 days
    "swing":       45 * 24 * 120,   # 45 days
}
```

**Status:** ✅ Fixed

---

### NEW-6 — SignalBus fire-and-forget task leak  
**Severity:** 🟡 Medium  
**File:** `api/signal_bus.py` — `_execute_async()`

**Problem:**  
`asyncio.create_task()` calls were not tracked, so tasks could be silently
garbage-collected before completing if no reference was held.

**Fix:**  
`self._active_tasks: set[asyncio.Task]` tracks all live tasks. Each task is
added on creation and removed via `add_done_callback`.

**Status:** ✅ Fixed

---

### NEW-7 — Runner-loop task accumulation under high MT5 latency  
**Severity:** 🟡 Medium  
**File:** `api/runner_loop.py` — `_runner_loop()`

**Problem:**  
If an MT5 call took longer than the 5-second base tick, a new strategy-runner
task would be spawned before the previous one finished, causing duplicate
signals and MT5 lock contention.

**Fix:**  
`_mode_tasks: dict[str, asyncio.Task]` stores the latest task per mode. Each
tick checks `if _prev and not _prev.done(): continue` before spawning a new task.

**Status:** ✅ Fixed

---

### NEW-8 — `TradeMemory._load()`: corrupt JSONL line drops remaining entries  
**Severity:** 🟡 Medium  
**File:** `ai/trade_memory.py` — `_load()`

**Problem:**  
A single malformed JSON line in `trade_journal.jsonl` would raise an exception
that aborted processing of all subsequent lines, silently losing trade history.

**Fix:**  
Each line is wrapped in its own `try/except json.JSONDecodeError`. Corrupt lines
are logged as a warning and skipped; the rest of the file loads normally.

**Status:** ✅ Fixed

---

### NEW-9 — RL Q-table not persisted at shutdown  
**Severity:** 🟡 Medium  
**File:** `ai/rl_agent.py`, `api/main.py`

**Problem:**  
The RL agent's in-memory Q-table updates were not guaranteed to reach disk if
the API process was killed mid-session (e.g. SIGTERM from NSSM service restart).

**Fix:**  
`RLAgentManager.shutdown()` calls `agent.save()` for all loaded agents.
`api/main.py` lifespan teardown calls `_rl_manager.shutdown()` unconditionally
before the process exits.

**Status:** ✅ Fixed

---

### NEW-10 — `news_filter._SYMBOL_CURRENCIES` incomplete  
**Severity:** 🟢 Low  
**File:** `engine/news_filter.py`

**Problem:**  
Stocks and crypto symbols listed only `["USD"]` as affected currencies, missing
EUR/JPY/BTC base-currency correlations.

**Fix:**  
- Crypto-to-crypto pairs (ETHBTC, LTCBTC, XRPBTC) now correctly map both
  constituent currencies
- EU/UK/APAC index symbols map to their home currencies (EUR, GBP, JPY, AUD)
- Broker-suffix stripping handles XM-specific variants (e.g. `EURUSD.r`)

**Status:** ✅ Fixed

---

### INFRA-1 — No CI pipeline  
**Severity:** 🟢 Low

**Problem:**  
No automated test runner; regressions could be introduced undetected.

**Fix:**  
`.github/workflows/ci.yml` added — runs `pytest tests/` on every push and pull
request to `main`. See [CI workflow](../.github/workflows/ci.yml).

**Status:** ✅ Fixed

---

### INFRA-2 — Audit findings not documented  
**Severity:** 🟢 Low

**Problem:**  
Audit results lived only in the upgrade roadmap; findings and their resolutions
were not tracked in a dedicated document.

**Fix:**  
This document (`docs/21-audit-findings.md`).

**Status:** ✅ Fixed

---

## Still Open (Genuine — Not Blocking)

| ID | Area | Severity | Notes |
|----|------|----------|-------|
| STRATEGY-1 | Scalping WR 15.38% | 🟠 High | Strategy tuning issue — not a code bug. Modes are independent; swing at 50% WR. |
| MULTI-USER | Tasks 19, 21, 22, 23 | 🟡 Medium | Schema + auth layer ready; DB not yet primary. Activate when multi-user needed. |
