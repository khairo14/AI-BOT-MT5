# AI-BOT-MT5 — Audit Findings

**Last Updated:** April 23, 2026  
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
| APR-1 | vol_confirm_mult blocks all signals | 🔴 Critical | ✅ Fixed |
| APR-2 | tp1_rr=1.0 fails R:R check for macd/index | 🟠 High | ✅ Fixed |
| APR-3 | sr_breakout retest_mode blocks all SR signals | 🔴 Critical | ✅ Fixed |
| APR-4 | Scanner auto-scan never writes to scanner.json | 🟠 High | ✅ Fixed |
| APR-5 | Scanner allows crypto in day_trading mode | 🟡 Medium | ✅ Fixed |
| APR-6 | ema_scalp exact single-bar crossover too strict | 🟡 Medium | ✅ Fixed |
| APR-7 | Regime TF for scalping was M5 (unstable ADX) | 🟠 High | ✅ Fixed |
| APR-8 | Regime gate mismatches (bb_squeeze, stoch_rsi) | 🟠 High | ✅ Fixed |
| APR-9 | is_trading_allowed checked after OHLCV fetch | 🟡 Medium | ✅ Fixed |
| APR-10 | account_info fetched N×M times per bar | 🟡 Medium | ✅ Fixed |
| APR-11 | swing vol_confirm_mult missing in strategies.json | 🟠 High | ✅ Fixed |

All findings resolved.

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

## April 23, 2026 — Signal Pipeline Audit

### APR-1 — `vol_confirm_mult: 1.2` blocks 100% of signals  
**Severity:** 🔴 Critical  
**Files:** `config/strategies.json`, all 5 strategy files with DEFAULT_PARAMS

**Problem:**  
MT5 tick volume is relative (not absolute), making cross-bar volume ratios
unreliable. Every strategy had `vol_confirm_mult: 1.2` in DEFAULT_PARAMS and
was not overridden in strategies.json. Since MT5 tick volume rarely exceeds
1.2× the 20-bar average in a consistent way, this silently blocked all signals.

**Fix:**  
Added `"vol_confirm_mult": 0` to all strategy param entries in strategies.json:
`ema_scalp`, `bb_squeeze`, `stoch_rsi_pullback`, `macd_ema_trend`, `sr_breakout`.
Each strategy guards with `if vol_mult > 0:` so `0` effectively disables the check.

**Status:** ✅ Fixed

---

### APR-2 — `tp1_rr: 1.0` fails R:R check for `macd_ema_trend` on indices  
**Severity:** 🟠 High  
**File:** `config/strategies.json`

**Problem:**  
`macd_ema_trend` had `tp1_rr: 1.0` (1:1 R:R). The validator enforces a minimum
1.5 R:R for day_trading, so every macd signal was silently rejected after all
the expensive OHLCV + regime + strategy computation ran.

**Fix:**  
`tp1_rr` changed to `1.5` for `macd_ema_trend` in strategies.json.

**Status:** ✅ Fixed

---

### APR-3 — `sr_breakout.retest_mode: true` blocks all SR signals  
**Severity:** 🔴 Critical  
**File:** `config/strategies.json`

**Problem:**  
`retest_mode: true` required price to return and retest the breakout level
before confirming. Under normal market conditions, fewer than 30% of breakouts
retested, so 70%+ of valid SR signals were discarded.

**Fix:**  
`retest_mode: false` in strategies.json (committed aa1c555).

**Status:** ✅ Fixed

---

### APR-4 — Scanner auto-scan results never written to `scanner.json`  
**Severity:** 🟠 High  
**File:** `api/main.py`

**Problem:**  
`_market_scanner_loop()` ran the scan and logged results but never persisted
them back to `scanner.json`, so the runner loop always used the manually-set
static symbol list regardless of market conditions.

**Fix:**  
`_update_scanner_json(summary)` added to `api/main.py`. Called after every scan.
Respects `auto_update_scanner` flag in `market_scanner.json` and `manual_override`
per-mode in `scanner.json`. Writes atomically (`.tmp` → rename).

**Status:** ✅ Fixed

---

### APR-5 — Scanner lets crypto/stocks appear in `day_trading` scanner  
**Severity:** 🟡 Medium  
**File:** `config/market_scanner.json`, `engine/market_scanner.py`

**Problem:**  
The scanner ranked symbols purely on volatility/spread metrics, promoting
crypto (ETHBTC) and exotic stocks into the day_trading slot where no strategy
was designed to trade them.

**Fix:**  
`allowed_categories` per trading_type in `market_scanner.json`:
- `scalping`: `["forex", "us_index"]`
- `day_trading`: `["forex", "us_index", "eu_index", "commodity"]`
- `swing`: all categories

`_scan_trading_type()` in `market_scanner.py` filters by category before scoring.

**Status:** ✅ Fixed

---

### APR-6 — `ema_scalp` requires exact single-bar EMA crossover  
**Severity:** 🟡 Medium  
**File:** `engine/strategies/scalping/ema_scalp.py`, `config/strategies.json`

**Problem:**  
The original crossover check required the crossover to occur on exactly the
current bar (`iloc[-1]`). If the runner loop was delayed by a slow MT5 call,
the crossover on bar N-1 was already missed and the strategy fired no signal
even though the EMA alignment was still valid.

**Fix:**  
Crossover detection changed to a sliding window: checks `range(-window, 0)`
for any bar where `ema_fast` crossed `ema_slow`. Still requires price to be
above/below both EMAs at the current bar to prevent re-triggering stale signals.
`crossover_window: 3` (15 min at M5) added to `strategies.json`.

**Status:** ✅ Fixed

---

### APR-7 — Regime classification used M5 for scalping (unstable ADX)  
**Severity:** 🟠 High  
**File:** `engine/strategy_runner.py` — `_PRIMARY_TF`

**Problem:**  
M5 ADX(14) covers only 70 minutes of history and flips label every few bars,
randomly classifying trending markets as `ranging` or `quiet` and blocking
valid scalp signals on trend-following strategies.

**Fix:**  
`_PRIMARY_TF` set to `"H1"` for all scalping strategies: `ema_scalp`,
`bb_squeeze`, `vwap_reversion`, `stoch_rsi_pullback`. H1 ADX(14) covers 14
hours and gives a stable regime label. `TIMEFRAME_BARS` already fetched H1
for all scalping strategies.

**Status:** ✅ Fixed

---

### APR-8 — Regime gate misconfigured for `bb_squeeze` and `stoch_rsi_pullback`  
**Severity:** 🟠 High  
**File:** `engine/strategy_runner.py` — `_REGIME_STRATEGIES`

**Problem:**  
- `bb_squeeze` was missing from the `quiet` whitelist — the one regime where
  it should fire (squeeze forms in quiet, breaks out on expansion).
- `stoch_rsi_pullback` was missing from `ranging_low_vol` and `ranging_high_vol`
  — its EMA-stack pullback logic works equally well in ranging markets.

**Fix:**  
`_REGIME_STRATEGIES` corrected:
- `quiet`: `{"bb_squeeze", "vwap_reversion"}`
- `ranging_low_vol`: added `stoch_rsi_pullback`
- `ranging_high_vol`: added `stoch_rsi_pullback`, `ema_scalp`

**Status:** ✅ Fixed

---

### APR-9 — `is_trading_allowed()` called after OHLCV fetch  
**Severity:** 🟡 Medium  
**File:** `engine/strategy_runner.py` — `_run_strategy()`

**Problem:**  
The circuit-breaker check (`is_trading_allowed`) was called mid-pipeline, after
fetching all OHLCV timeframes. When trading was halted for a mode, every
symbol × strategy combination still performed up to 3 MT5 API calls before
being blocked.

**Fix:**  
`is_trading_allowed` moved to the very first check in `_run_strategy()`, before
any data fetch. Avoids up to 28 redundant OHLCV fetches per blocked bar.

**Status:** ✅ Fixed

---

### APR-10 — `account_info` fetched N×M times per bar  
**Severity:** 🟡 Medium  
**File:** `engine/strategy_runner.py` — `run_mode()`, `_run_strategy()`

**Problem:**  
Each `_run_strategy()` call independently fetched `get_account_info()` from
MT5. For 7 symbols × 4 strategies = 28 calls per bar, each incurring MT5 IPC
round-trip overhead.

**Fix:**  
`run_mode()` fetches account info once into `_mode_account`, then passes it as
`_account=_mode_account` to every `_run_strategy()` call. Falls back to a
per-call fetch if the pre-fetch fails.

**Status:** ✅ Fixed

---

### APR-11 — Swing strategies `ema_trend_rider` and `weekly_breakout` missing `vol_confirm_mult: 0`  
**Severity:** 🟠 High  
**File:** `config/strategies.json`

**Problem:**  
The April fix for `vol_confirm_mult` (APR-1) added the override to scalping and
day_trading strategies but missed the two swing strategies that also use volume
confirmation. `ema_trend_rider` and `weekly_breakout` both have
`vol_confirm_mult: 1.2` in DEFAULT_PARAMS with no strategies.json override,
blocking all swing signals from those strategies.

Found during full audit pass (April 23, 2026) by cross-checking grep results
for `vol_confirm_mult` across all strategy files against strategies.json params.

**Fix:**  
Added `"vol_confirm_mult": 0` to both entries in `strategies.json`:
- `swing.params.ema_trend_rider`
- `swing.params.weekly_breakout`

**Status:** ✅ Fixed

---

## Still Open (Genuine — Not Blocking)

| ID | Area | Severity | Notes |
|----|------|----------|-------|
| STRATEGY-1 | Scalping WR 15.38% | 🟠 High | Strategy tuning issue — not a code bug. Modes are independent; swing at 50% WR. |
| MULTI-USER | Tasks 19, 21, 22, 23 | 🟡 Medium | Schema + auth layer ready; DB not yet primary. Activate when multi-user needed. |
