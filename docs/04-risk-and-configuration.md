# EVOTRADE-AI — Risk Management & Configuration

**Last Updated:** April 2026

---

## Philosophy

**Capital security comes first.** Every setting below is designed around: **survive first, profit second.** The risk engine operates at multiple levels — per-trade sizing, concurrent limits, correlation guards, circuit breakers, news filters, and session filters — all active simultaneously.

---

## Per-Trade Risk

| Parameter | Default | Description |
| --- | --- | --- |
| `risk_per_trade_pct` | `1.0` | % of current account balance risked per trade |
| `max_risk_per_trade_pct` | `2.0` | Hard ceiling — no single trade can exceed this |
| `risk_reward_min` | `1.5` | Minimum R:R ratio; signals below this are discarded before scoring |
| `sl_required` | `true` | Enforced in order_manager.py at code level — not just config |

**Position size formula (MT5-native):**

```text
sl_ticks    = abs(entry - sl) / tick_size
risk_amount = balance × risk_pct / 100
lot         = floor(risk_amount / (sl_ticks × tick_value) / lot_step) × lot_step
```

Floor-rounded (never rounds up — avoids over-risking). Warns in logs when broker `min_lot` forces actual risk above intended.

**Additional lot sizing layers applied in order:**

1. Base lot from formula above
2. RL risk_factor multiplier (×0.60–1.50)
3. Volatility adjustment (ATR% vs baseline, up to ×0.50 reduction)
4. Regime-based reduction (×0.75–1.00)

---

## Concurrent Trade Limits

| Mode | Default Max | Configurable |
| --- | --- | --- |
| Scalping | 3 | 1–10 |
| Day Trading | 5 | 1–15 |
| Swing | 8 | 1–20 |
| **Total** | **12** | 1–30 |

Per-symbol limit is also enforced within each mode. A new signal for a symbol that already has an open position in the same mode is blocked before execution.

---

## Correlation Guard

Prevents compounding losses when a macro event (DXY spike, crypto liquidation cascade, index sell-off) hits multiple correlated positions simultaneously.

**8 asset groups — direction-aware:**

| Group | Examples |
| --- | --- |
| USD Short (BUY weakens USD) | EURUSD, GBPUSD, AUDUSD, NZDUSD, GOLD |
| USD Long (BUY strengthens USD) | USDJPY, USDCAD, USDCHF |
| Crypto | BTCUSD, ETHUSD, XRPUSD, SOLUSD |
| Gold / Silver | GOLD, SILVER, XAUUSD, XAGUSD |
| Oil / Energy | USOIL, BRENTCash, NGASCash |
| US Indices | US30Cash, US100Cash, US500Cash |
| EU / Asia Indices | GER40Cash, UK100Cash, FRA40Cash |
| Tech Stocks | Tesla, Nvidia, Apple, Microsoft, Amazon, Meta |

`max_correlated_positions` (default 1) in `app.json` is the per-group-per-mode limit. Cross-mode stacking is allowed — a scalping BUY and a swing BUY on EURUSD are independent.

---

## Drawdown Circuit Breakers

| Parameter | Default | Description |
| --- | --- | --- |
| `daily_limit_pct` | `5.0` | Daily loss ≥ 5% of day-start balance → all modes pause until midnight UTC |
| `weekly_limit_pct` | `10.0` | Weekly loss ≥ 10% → all modes pause until Monday UTC |
| `max_consecutive_losses` | `5` | Per-mode: 5 consecutive losses → mode pauses for `consecutive_loss_pause_hours` |
| `consecutive_loss_pause_hours` | `4` | Auto-resume after this many hours |

**Daily reset:** UTC midnight (not local server time — ensures correct reset for all timezones).

**Weekly reset:** Uses `(iso_year, iso_week)` tuple — safe across the year boundary (e.g. Jan 1 in ISO week 52 of prior year does not skip a weekly reset).

**Persistence:** Circuit breaker state (halted flags, balance anchors, consecutive loss counters, paused modes) is saved to `data/risk_state.json` after every update and restored on restart. A server crash does not clear accumulated drawdown context.

**Behaviour when triggered:**

- No new trades are opened
- Existing open trades are NOT forcibly closed — they run to natural SL/TP
- Circuit breaker events are broadcast to all connected dashboard clients via WebSocket
- Manual reset available via dashboard Settings or `POST /risk/reset-drawdown`

---

## News Filter

The bot checks the Forex Factory economic calendar before opening any trade affected by the filter.

| Parameter | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Globally enable/disable |
| `pause_minutes_before` | `30` | Pause new entries 30 min before high-impact events |
| `pause_minutes_after` | `15` | Resume 15 min after the event |
| `impact_filter` | `"high"` | Filter by: `"high"`, `"medium"`, `"low"` |
| `affect_modes` | `["scalping", "day_trading"]` | Swing trades are not paused by news |

**Calendar coverage:** Current week (guaranteed) + next week (attempted via date-based URL; silently skipped if unavailable). Events are deduplicated across both weeks. This prevents Sunday-night and early-Monday events from being invisible.

**Earnings blackout:** Stock CFD symbols (Tesla, NVDA, etc.) are checked against a separate Nasdaq earnings calendar. Trading pauses 2 hours before earnings and 4 hours after.

**DST-aware timezone:** Forex Factory publishes in US/Eastern time. The filter converts using `ZoneInfo("America/New_York")` (DST-aware). Falls back to manual DST rule calculation if `tzdata` is unavailable.

---

## Session Filter

Certain instruments should not be traded outside their active exchange hours.

| Instrument | Active Hours (UTC) | Bot Behaviour |
| --- | --- | --- |
| Forex Majors | 07:00–21:00 Mon–Fri | Scalp/day signals suppressed |
| US Stock CFDs | 14:30–21:00 Mon–Fri | Signals suppressed |
| EU Indices (GER40) | 08:00–16:30 Mon–Fri | Signals suppressed |
| UK Indices (UK100) | 08:00–16:30 Mon–Fri | Signals suppressed |
| Commodities | 01:00–22:00 Mon–Fri | Wide spreads outside hours |
| Cryptocurrency | 00:00–23:59 every day | No restriction |

**US market holidays:** Full NYSE/NASDAQ holiday calendar (New Year's, MLK, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas) is enforced for stock and US index instruments. Holiday dates are computed programmatically using the Gregorian algorithm — no hardcoded list.

**Session close (exclusive boundary):** At exactly `close_time` the session is treated as closed. This prevents a trade opening in the last second of a session.

**Symbol category refresh:** The session filter reloads `symbols.json` every 5 minutes so newly added symbols pick up the correct category without a restart.

---

## Live Spread Gate

Before placing any order, the current live spread is fetched from MT5 and compared against per-mode limits:

```json
"max_spread_pips": {
  "scalping":    1.5,
  "day_trading": 3.0,
  "swing":       5.0
}
```

If the current spread exceeds the limit, the signal is rejected with `"Spread too wide"` and the lot-revalidation block retries with fresh symbol info. This protects against entering during news spikes when spreads can reach 8–15 pips.

---

## Signal Expiry

Pending manual signals expire after one bar's worth of time for their timeframe:

| Timeframe | Expiry |
| --- | --- |
| M1 | 60 s |
| M5 | 300 s |
| H1 | 3600 s |
| H4 | 14400 s |
| D1 | 86400 s |

When a signal expires, its status is set to `"expired"` and broadcast to all dashboard clients. The Approve button becomes inactive. Expired signals accumulate in the archive (last 500) and are visible in the signal history panel.

---

## SL/TP Reanchoring

When a signal is generated, entry/SL/TP are calculated from the bar's close price. By the time the order is placed (especially for scalping), the live tick may have moved. Order manager reanchors SL/TP to the live fill price before sending:

```text
sl_dist = abs(signal_entry - signal_sl)
live_sl = live_price - sl_dist  (for BUY)
live_tp = live_price + tp_dist  (for BUY)
```

This prevents "Invalid stops" broker rejections and stops the SL from being hit immediately after fill.

---

## Trailing Stops & Partial Close (Automatic, Position Lifecycle)

Applied by `_poll_outcome` which monitors every open position every 30 seconds:

**Day Trading positions:**

1. When price hits TP1: close 50% of position, move SL to entry (breakeven)
2. After TP1: trail remaining position with ATR(14, H1) × 1.5. If ATR unavailable, trail at 50% of original SL distance.

**Swing positions:**

1. When price reaches 50% of TP distance: move SL to entry (breakeven)
2. After breakeven: trail with ATR(14, H4) × 2.0. Falls back to 50% of original SL distance.

**SL movement rule:** SL only moves in the profitable direction — never widened. `modify_position` is called only when the new SL improves on the current SL.

---

## Weekend Gap Protection

On Fridays between 20:45–21:00 UTC, the bot checks for open swing positions. If any are found and `weekend_gap_protection = true` in `app.json`, they are closed to avoid gap risk over the weekend. Scalping and day-trading positions are not affected (they are normally closed within the trading day anyway).

---

## Full Configuration Reference

### `config/risk.json`

```json
{
  "risk_per_trade_pct": 1.0,
  "max_risk_per_trade_pct": 2.0,
  "risk_reward_min": 1.5,
  "risk_reward_min_by_mode": {
    "scalping": 1.2,
    "day_trading": 1.5,
    "swing": 2.0
  },
  "sl_required": true,

  "max_concurrent_trades": {
    "scalping": 3,
    "day_trading": 5,
    "swing": 8,
    "total": 12,
    "per_symbol": 1
  },

  "drawdown": {
    "daily_limit_pct": 5.0,
    "weekly_limit_pct": 10.0,
    "max_consecutive_losses": 5,
    "consecutive_loss_pause_hours": 4
  },

  "news_filter": {
    "enabled": true,
    "pause_minutes_before": 30,
    "pause_minutes_after": 15,
    "impact_filter": "high",
    "affect_modes": ["scalping", "day_trading"],
    "cache_minutes": 60
  },

  "session_filter": {
    "enabled": true,
    "sessions": {
      "forex":      { "open": "07:00", "close": "21:00", "days": "mon-fri" },
      "us_stocks":  { "open": "14:30", "close": "21:00", "days": "mon-fri" },
      "us_indices": { "open": "14:30", "close": "21:00", "days": "mon-fri" },
      "eu_indices": { "open": "08:00", "close": "16:30", "days": "mon-fri" },
      "commodities":{ "open": "01:00", "close": "22:00", "days": "mon-fri" },
      "crypto":     { "open": "00:00", "close": "23:59", "days": "all" }
    }
  }
}
```

### `config/app.json` (key fields)

```json
{
  "trading_mode": "paper",
  "execution_mode": {
    "scalping":    "manual",
    "day_trading": "manual",
    "swing":       "manual"
  },
  "max_correlated_positions": 1,
  "weekend_gap_protection": true,
  "max_spread_pips": {
    "scalping":    1.5,
    "day_trading": 3.0,
    "swing":       5.0
  },
  "ai": {
    "price_prediction_enabled": true,
    "confidence_filter_enabled": false,
    "confidence_threshold": 60,
    "rl_agent_enabled": true,
    "scorer_weights":          { "lstm": 0.30, "rr": 0.35, "trend": 0.20, "volume": 0.15 },
    "scalping_scorer_weights": { "lstm": 0.15, "rr": 0.25, "trend": 0.40, "volume": 0.20 },
    "swing_scorer_weights":    { "lstm": 0.40, "rr": 0.30, "trend": 0.25, "volume": 0.05 },
    "regime_weights": {
      "trending_bull":     { "lstm": 0.35, "rr": 0.25, "trend": 0.30, "volume": 0.10 },
      "trending_bear":     { "lstm": 0.35, "rr": 0.25, "trend": 0.30, "volume": 0.10 },
      "ranging_low_vol":   { "lstm": 0.25, "rr": 0.40, "trend": 0.10, "volume": 0.25 },
      "ranging_high_vol":  { "lstm": 0.20, "rr": 0.45, "trend": 0.05, "volume": 0.30 },
      "volatile_breakout": { "lstm": 0.25, "rr": 0.40, "trend": 0.20, "volume": 0.15 },
      "quiet":             { "lstm": 0.35, "rr": 0.30, "trend": 0.20, "volume": 0.15 }
    }
  }
}
```

---

## Config Schema Validation

Every `PATCH` request to any config endpoint is validated before writing:

- **Type checks:** numeric fields must be numbers, boolean fields must be true/false (not strings)
- **Range checks:** `risk_per_trade_pct` must be 0.01–10.0; `daily_limit_pct` must be 0.1–50.0, etc.
- **Cross-field checks:** `daily_limit_pct` cannot exceed `weekly_limit_pct`
- **Scorer weight sum:** weights in any weight block must sum to 1.0 ± 5%
- **Strategy name validation:** `active_strategies` lists must contain only known strategy names
- **Symbol list validation:** each entry must have a string `symbol` and a boolean `enabled`
- **Scanner symbol type:** must be a list of strings

**Health endpoint:** `GET /config/validate` reads all 6 config files and reports any JSON parse errors or missing files. Useful for diagnosing silent corruption after a bad manual edit.

---

## Capital Security Summary

The following protections are always active and cannot be disabled from the UI:

1. **SL on every trade** — `sl_required: true` enforced at order manager code level
2. **Daily/weekly drawdown circuit breaker** — checked before every new order
3. **Max position size cap** — `max_risk_per_trade_pct` is a hard ceiling
4. **No trade without MT5 connection** — watchdog reconnects, but no order sent without confirmed connection
5. **Lot size sanity check** — validated against `min_lot` and `max_lot` before sending; zero-lot signals are rejected
6. **Duplicate order guard** — per-symbol per-mode dedup at both SignalBus and OrderManager levels
7. **Correlation guard** — maximum 1 correlated position per asset group per mode (configurable)
8. **News blackout** — no new entries within configurable window around high-impact events
9. **Session filter** — no signals outside instrument-specific market hours
10. **Live spread gate** — no entry when current spread exceeds mode-specific limit
