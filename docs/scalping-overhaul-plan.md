# Scalping Strategy Overhaul Plan

**Status**: Approved — pending execution  
**Date**: 2026-03-26  
**Trigger**: 193-trade paper analysis showing 19.2% win rate, -$6,259 P&L, avg RR 2.58

## Root Cause

The scalping strategies were using day-trade-sized TP targets (RR 2.0–2.5) at M1/M5 frequency.
Break-even win rate for RR 2.58 = ~28%. Actual win rate = 19.2%. **Structurally unprofitable.**

---

## Task 1 — Strategy Code Fixes

### `engine/strategies/scalping/ema_scalp.py`
- **Remove `bias == "NONE"` bypass**: Both BUY and SELL conditions currently allow entry when
  M5 EMA50 bias is `"NONE"` (no confirmed direction). Remove this — require explicit `BULL`/`BEAR`.
- **Enforce `max_spread_pips` check**: The param exists in `DEFAULT_PARAMS` (`1.5`) but is never
  applied in code. Add spread guard before signal is emitted.
- **Tighten RSI range**: Buy requires `rsi_min ≥ 52` (not 40). Entering on RSI 40 is flat neutral
  territory. Require actual bullish momentum.

### `engine/strategies/scalping/vwap_reversion.py`
- **Tighten RSI thresholds**: `rsi_oversold 35 → 28`, `rsi_overbought 65 → 72`.
  Only enter on genuinely extreme readings, not moderate drift.

### `engine/strategies/scalping/bb_squeeze.py`
- No code changes — issues are param-only.

---

## Task 2 — Config Changes (`config/optimized_params.json`)

### `ema_scalp` — all FX scalping symbols
- Lower `rr`: `2.0 → 1.2` for EURUSD, USDJPY, GBPUSD, EURJPY, USDCHF
- This drops break-even WR from 33% to 45% — wait, let me recalc:
  - RR 1.2 → break-even = 1/(1+1.2) = **45%** — still needs work on signal quality
  - RR 1.0 → break-even = **50%** — pure coin flip, too tight
  - **Target**: RR 1.2–1.3, aim for 40–45%+ win rate with tighter entries

### `bb_squeeze` — all FX symbols
- `min_squeeze_bars`: `3 → 5` in `__global__` (3 M5 bars = 15min is not a real squeeze)
- `tp_atr_mult`: `3.0 → 1.5` for all FX symbol entries (3 ATR TP is unreachable at M5 scale)

---

## Task 3 — Separate Scorer Weights for Scalping (`config/app.json`)

Add `scalping_scorer_weights` block alongside existing `scorer_weights`:

```json
"scalping_scorer_weights": {
  "lstm": 0.15,
  "rr":   0.25,
  "trend": 0.40,
  "volume": 0.20
}
```

**Rationale**: Current `scorer_weights` (`lstm: 0.30`) over-weights LSTM on scalping. LSTM models
are trained on M15/H1+ data; their confidence on M1/M5 is less reliable. Trend confirmation
(EMA direction, momentum) is the primary edge in scalping — should carry the most weight.

**Code impact**: `signal_scorer.py` needs to read `scalping_scorer_weights` when `trading_type == "scalping"`.

---

## Task 4 — Data Reset

### RL Agent Q-table
- **Wipe**: `ai/data/rl_qtable_scalping_paper.json`
- **Reason**: Q-table was trained on 193 losing trades with wrong RR setup.
  The agent learned to block/de-risk scalping signals (conf_thresh raised to 0.54).
  Starting fresh at defaults (`conf_thresh=0.55`, `risk_factor=1.00`) lets it learn
  from the corrected strategy geometry.
- **Leave untouched**: `rl_qtable_day_trading_paper.json`, `rl_qtable_swing_paper.json`

### Trade Memory
- **Clear scalping entries from**: `ai/data/trade_memory.jsonl`
- **Reason**: Trade memory scorer uses historical signal outcomes to adjust confidence.
  Scalping entries all reflect the broken RR setup and will bias new signals negatively.
- **Method**: Filter out all `"trading_type": "scalping"` rows, keep day_trading and swing rows.

---

## Task 5 — Account Switch & Data Reset

### 5a. Backup (before touching anything)
Copy the current trade journal to an archive directory so historical data is preserved:

```
data/archive/trade_journal_paper_v1_2026-03-26.jsonl   ← copy of data/trade_journal.jsonl
```

The analytics dashboard reads **only** from `data/trade_journal.jsonl` on every refresh.
Once that file is cleared, the analytics page will show zero trades — it has no secondary DB or cache.
The backup above preserves the full history for future reference.

### 5b. Files to clear / reset (in order, bot stopped)

| File | Action | Reason |
|---|---|---|
| `data/trade_journal.jsonl` | **Wipe** (after backup) | All analytics data — fresh account = fresh history |
| `ai/data/trade_memory.jsonl` | **Wipe** | Scorer outcome memory biased by old broken setups |
| `ai/data/rl_qtable_scalping_paper.json` | **Delete** | RL unlearns 193-trade loss history from wrong RR |
| `ai/data/rl_qtable_day_trading_paper.json` | **Delete** | Fresh account = fresh RL learning |
| `ai/data/rl_qtable_swing_paper.json` | **Delete** | Same reason |
| `data/risk_state.json` | **Reset to defaults** | New account must not inherit old consecutive-loss counts or halt flags |

### 5c. Files to update

| File | What to change |
|---|---|
| `config/app.json` | Update `accounts.paper.login` with new demo account number |

### 5d. Files to leave alone

| File | Reason |
|---|---|
| `data/regime_state.json` | Reads live from market — not account-specific |
| `ai/data/optimizer_status.json` | Param optimiser state — not trade-account linked |
| `data/backtest_history/` | Backtests are symbol/strategy specific, not account specific |

---

## What Is NOT Changed

| Item | Reason Deferred |
|------|----------------|
| Remove EURJPY/USDJPY from scalping | Keeping for data gathering; will revisit for live |
| Time-of-day filter (07:00–17:00 UTC) | More invasive session_filter change; track separately |
| Retire `ema_scalp` entirely | Give it a chance after code + param fixes first |

---

## Post-Execution Validation

After changes are deployed and ~50+ new scalping trades accumulate:
1. Check analytics: Win rate should be trending toward 35%+
2. Check RL: `conf_thresh` should stabilize lower than 0.54 if signals are improving
3. Check trade quality: Should see some "High" quality signals appearing
4. If `ema_scalp` still 0%)+ after 30 trades → consider disabling
