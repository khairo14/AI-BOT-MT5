---
name: audit-system
description: 'Full system audit for AI-BOT-MT5. Use when: finding bugs, logic gaps, or regressions; reviewing a file or subsystem for correctness; checking concurrency safety, state persistence, math correctness, or config coverage; preparing for a production switch from paper to live; or doing a periodic health check. Produces a structured findings report with severity ratings and fix recommendations.'
argument-hint: 'scope of audit (e.g. "full", "signal_bus", "risk_manager", "math and lot sizing", "config coverage")'
---

# System Audit — AI-BOT-MT5

Produces a structured findings report. Findings are rated by severity and compared against the existing audit history in [docs/08-audit-findings.md](../../../docs/08-audit-findings.md) so duplicates are skipped and only new issues are reported.

---

## Audit Scope Options

| Scope keyword | Files covered |
|---|---|
| `full` | All subsystems below, in order |
| `risk_manager` | `engine/risk_manager.py`, `data/risk_state.json`, `config/risk.json` |
| `signal_bus` | `api/signal_bus.py`, `api/runner_loop.py` |
| `order_manager` | `engine/order_manager.py`, `engine/paper_trade.py` |
| `strategies` | `engine/strategy_runner.py`, `engine/strategies/`, `config/strategies.json` |
| `ai` | `ai/predictor.py`, `ai/rl_agent.py`, `ai/trade_memory.py`, `ai/signal_scorer.py` |
| `config coverage` | All `config/*.json` files cross-referenced against code usage |
| `math and lot sizing` | Lot calc in `signal_bus.py`, partial close in `order_manager.py`, scorer weights in `app.json` |
| `concurrency` | All `threading.Lock` / `asyncio.Lock` usage across engine + API |
| `persistence` | State files: `data/risk_state.json`, `data/regime_state.json`, `ai/data/*.json`, `ai/data/*.jsonl` |
| `backtester` | `engine/backtester.py`, `api/routes/backtest.py` |
| `filters` | `engine/news_filter.py`, `engine/session_filter.py` |
| `api routes` | All `api/routes/*.py` and `api/websocket/feed.py` |

---

## Step 1 — Load Existing Findings

Before scanning anything, read [docs/08-audit-findings.md](../../../docs/08-audit-findings.md) to know what has already been audited and fixed.

- Skip any issue with status ✅ Fixed
- Note items marked "Accepted" (known, deliberately not fixed)
- Use existing IDs as reference — new findings get the next sequential ID in each category

**Existing finding ID prefixes:**

- `C-` Concurrency/Critical
- `H-` High severity
- `M-` Medium severity
- `L-` Low severity
- `G-` Gap (missing feature or guard)
- `BUG-`, `LOGIC-`, `MATH-`, `GAP-`, `IMPROVE-` (Round 2 format)

For new findings, continue the `GAP-` / `BUG-` / `MATH-` / `LOGIC-` / `IMPROVE-` pattern with the next available number.

---

## Step 2 — Check Categories

For each file in scope, apply the relevant category checklist below. Read the actual source code — do not infer from docs.

### A. Concurrency Safety

- [ ] Every mutable shared state protected by `threading.Lock` or `asyncio.Lock`
- [ ] No `threading.Lock` used inside `async def` (use `asyncio.Lock`)
- [ ] No MT5 API calls (`mt5.*`) outside `with self._client._lock:`
- [ ] `asyncio.create_task()` fire-and-forget calls: are they safe to lose on shutdown?
- [ ] Module-level mutable vars accessed from both async loop and HTTP threads (e.g. `_paused` in `runner_loop.py`)

### B. State Persistence

- [ ] All circuit breaker, halt, and pause state written to `data/risk_state.json` on every mutation
- [ ] State correctly restored on restart (no `None` fields that bypass guards on first run)
- [ ] RL Q-tables saved on shutdown or within acceptable update window (current: every 10 updates)
- [ ] Trade memory JSONL: per-line error isolation during `_load()` (single bad line must not drop subsequent entries)
- [ ] Backtest results have a retention policy

### C. Math & Lot Sizing

- [ ] Lot rounding uses `math.floor(/ step) * step` — never `round()`
- [ ] `volume_min` clamp applied **after** floor-round (not before — over-close risk)
- [ ] SL distance > 0 before lot calculation (division-by-zero guard)
- [ ] `tick_value` and `tick_size` returned from MT5 before use (symbol not in Market Watch → `None`)
- [ ] LSTM accuracy gate: current floor is 58% — verify positive EV after spread at this threshold
- [ ] RL reward asymmetry: losses clipped to `-0.05`, profits to `+0.15`
- [ ] Trend score normalizer: gap threshold at 0.05 (not 0.02)
- [ ] Signal scorer weights sum check: `lstm + rr + trend + volume` weights should be verified for each regime

### D. Logic & Guard Correctness

- [ ] `_verify_paper_mode()` called at top of both `scan_and_signal()` and `place_paper_order()`
- [ ] `is_trading_allowed(trading_type)` checked before every order dispatch (RISK-5 pre-check)
- [ ] Signal age check before manual approval (`expires_at` re-checked at execution time)
- [ ] `_poll_outcome` MAX_POLLS cap: current `7 * 24 * 120 = 20,160` iterations (**swing trades > 7 days have close silently missed**)
- [ ] `close_all_positions()` closes by `magic == BOT_MAGIC` — confirms this is intentional
- [ ] Paper vs live circuit breakers are isolated (M-6 fix: no cross-contamination)
- [ ] Dedup key `(symbol, strategy, direction, trading_type)` — check for edge cases where strategy name changes between runs

### E. Configuration Coverage

- [ ] Every field read from `app.json` / `risk.json` / `strategies.json` / `scanner.json` has a documented default fallback in code
- [ ] No `app.json` keys read on hot paths without TTL cache (5 s TTL via `_get_app_config()`)
- [ ] `_SYMBOL_CURRENCIES` in `news_filter.py` is **hardcoded** — verify all active symbols from `config/scanner.json` are covered; symbols not in the dict silently fall back to USD-only news check
- [ ] Session filter times in `risk.json` are UTC — not DST-aware; US stocks/indices need manual update at DST transitions (second Sunday March / first Sunday November)

### F. API & WebSocket

- [ ] All `PATCH /config/*` endpoints validate input before writing to disk (no arbitrary JSON injection)
- [ ] `POST /trades/place` and `POST /signals/{id}/approve` both go through `is_trading_allowed()` pre-check
- [ ] WebSocket `broadcast_alert` fires on circuit breaker trigger (G-3 fix — verify still wired)
- [ ] `POST /account/switch-mode` with `force=true` — confirm existing open positions are handled safely

### G. Backtester Fidelity

- [ ] `initial_balance` parameter passed from live account balance (not hardcoded `10_000`)
- [ ] No concurrent-position cap modelled (positions can overlap unrealistically in backtest)
- [ ] No swap/commission model beyond `SPREAD_COST_R`
- [ ] Walk-forward only — no out-of-sample split; results are in-sample optimistic
- [ ] Strategy params sourced from `optimizer.get_params()` — if optimizer hasn't run, uses strategy defaults (undocumented assumption)

---

## Step 3 — Document Findings

For each new issue found, write an entry in this format:

```
### [NEW-ID] — [File] — [Severity]
**Category:** Concurrency / Math / Logic / Config / Gap / Performance
**Description:** One clear sentence of what the bug or gap is.
**Trigger condition:** When does this manifest?
**Risk:** None / Very Low / Low / Medium / High / Critical
**Recommended fix:** Specific code change or config addition.
```

**Severity guide:**

| Severity | Definition |
|---|---|
| Critical | Can cause data loss, wrong orders, or account damage |
| High | Silently wrong result, or loss of trade journal integrity |
| Medium | Degraded accuracy, stale data, or misleading metrics |
| Low | Minor logic inconsistency or technical debt |
| Very Low | Cosmetic, theoretical, or extremely rare edge case |

---

## Step 4 — Known Open Gaps (from prior audits, not yet fixed)

Cross-reference against these before creating duplicates:

| ID | File | Issue | Risk |
|---|---|---|---|
| NEW-1 | `engine/risk_manager.py` | `_day_start_balance` is `None` on fresh start — drawdown guard skipped until first `update_balance()` | Low |
| NEW-2 | `engine/risk_manager.py` | `check_concurrent_limit()` reads `_mode_switch_ts` without `_lock` | Very Low |
| NEW-3 | `engine/order_manager.py` | `partial_close` applies `volume_min` clamp before floor-round (could over-close on tiny lots) | Low |
| NEW-4 | `engine/paper_trade.py` | `self._history` list grows unbounded — no eviction or size cap | Low |
| NEW-5 | `api/signal_bus.py` | `_poll_outcome` exits after 7 days; swing trades > 7 days silently miss their close record | Medium |
| NEW-6 | `api/signal_bus.py` | `_execute_async` tasks are fire-and-forget; ungraceful shutdown can lose journal write for in-flight orders | Low |
| NEW-7 | `api/runner_loop.py` | `_run_one_mode` tasks accumulate if MT5 is slow — no cancellation or backpressure | Low |
| NEW-8 | `ai/trade_memory.py` | Single corrupt JSONL line drops all subsequent entries — no per-line resilience in `_load()` | Low |
| NEW-9 | `ai/rl_agent.py` | Up to 9 Q-table updates lost on ungraceful shutdown | Very Low |
| NEW-10 | `engine/news_filter.py` | `_SYMBOL_CURRENCIES` hardcoded and incomplete — stocks/crypto/commodities only check USD news | Low |
| LOGIC-2 | `api/signal_bus.py` | 5 s race window in dedup guard between same-symbol signals | Accepted |
| L-8 | `ai/param_optimizer.py` | Strategy map never invalidated across optimizer restarts | Accepted |

---

## Step 5 — Produce Report

Output a findings report with sections:

1. **Audit summary** — scope, files read, date
2. **New findings** — full entries per NEW-ID
3. **Confirmed open gaps** — which of the known gaps above were verified still open
4. **No-issue files** — list files explicitly checked with no new findings
5. **Recommended priority order** — sort new findings by risk: Critical → High → Medium → Low

After the report, ask: "Should I fix any of these now, or update `docs/08-audit-findings.md` with the new findings?"
