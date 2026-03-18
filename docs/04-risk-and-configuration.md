# AI-BOT-MT5 — Risk Management & Configuration

## Philosophy

**Capital security comes first.** A strategy that wins 70% of the time but loses 20% of the account on a bad day is worse than a strategy that wins 55% but never risks more than 1% per trade. Every setting below is designed around the principle: **survive first, profit second.**

---

## Risk Parameters (Defaults — Bot Managed)

All defaults are pre-tuned for a conservative-to-moderate risk profile. Every value is configurable from the dashboard Settings panel, stored in `config/risk.json`.

### Per-Trade Risk

| Parameter | Default | Description |
|---|---|---|
| `risk_per_trade_pct` | `1.0` | % of current account balance risked per trade |
| `max_risk_per_trade_pct` | `2.0` | Hard ceiling — no single trade can exceed this |
| `risk_reward_min` | `1.5` | Minimum R:R ratio; signals below this are discarded |
| `sl_required` | `true` | No trade is opened without a stop-loss. Non-negotiable |
| `tp_required` | `false` | TP is recommended but can trade with trailing stop only |

> **Position size is auto-calculated** from `risk_per_trade_pct`, account balance, entry price, and SL distance. You never manually set lot sizes — the bot does it.

```
Lot Size = (Account Balance × Risk %) / (SL Distance in pips × Pip Value)
```

---

### Concurrent Trade Limits (Bot-Managed Defaults)

| Trading Type | Default Max Open Trades | Configurable Range |
|---|---|---|
| Scalping | 3 | 1–10 |
| Day Trading | 5 | 1–15 |
| Swing Trading | 8 | 1–20 |
| **Total Across All Modes** | **12** | 1–30 |

The bot will not open a new trade in a mode if the concurrent limit is reached. The signal is logged but not executed until a slot opens.

---

### Drawdown Protection

| Parameter | Default | Description |
|---|---|---|
| `daily_drawdown_limit_pct` | `5.0` | If daily loss reaches 5% of starting-day balance, all modes pause for the rest of the day |
| `weekly_drawdown_limit_pct` | `10.0` | If weekly loss reaches 10%, bot pauses until Monday and sends alert |
| `max_consecutive_losses` | `5` | After 5 consecutive losses on any mode, that mode pauses for 4 hours |
| `drawdown_action` | `"pause"` | Options: `"pause"` (default) or `"reduce_risk"` (halves risk % instead of pausing) |

> When the circuit breaker triggers, **no new trades are opened** but **existing open trades are not forcibly closed** — they run to their natural SL/TP. This prevents panic-closing at bad prices.

---

### Position Sizing Method

| Method | Description | When to Use |
|---|---|---|
| `fixed_fractional` (default) | Risks the same % each trade regardless of recent performance | Stable, predictable — recommended default |
| `kelly_criterion` | Sizes position based on win rate × average win:loss ratio | More aggressive — activate only after 50+ trades of data |

Configure via `config/risk.json`:
```json
{ "position_sizing_method": "fixed_fractional" }
```

---

### News Filter

The bot checks an economic calendar (Forex Factory XML feed or investing.com API) before opening any Forex or index trade.

| Parameter | Default | Description |
|---|---|---|
| `news_filter_enabled` | `true` | Globally enable/disable |
| `news_pause_minutes_before` | `30` | Pause new trades 30 min before high-impact event |
| `news_pause_minutes_after` | `15` | Resume 15 min after the event |
| `news_impact_filter` | `"high"` | Filter by: `"high"`, `"medium_high"`, `"all"` |
| `news_affect_modes` | `["scalping", "day_trading"]` | Swing trades are not paused by news (hold through) |

> Note: Crypto and stock CFDs are not paused by Forex news events unless they are correlated (e.g., NFP may affect US indices).

---

### Session Filters

Certain instruments should not be traded outside their active sessions.

| Instrument Type | Active Hours (UTC) | Bot Behavior Outside Hours |
|---|---|---|
| Forex Majors | 07:00–21:00 Mon–Fri | Scalp/day signals suppressed |
| US Stock CFDs | 14:30–21:00 Mon–Fri | Signals suppressed |
| EU Indices (GER40) | 08:00–16:30 Mon–Fri | Signals suppressed |
| UK Indices (UK100) | 08:00–16:30 Mon–Fri | Signals suppressed |
| Commodities | 01:00–22:00 Mon–Fri | Wide spread outside these hours |
| Cryptocurrency | 00:00–23:59 every day | No restriction |

---

## Full Configuration Reference

### `config/risk.json`

```json
{
  "risk_per_trade_pct": 1.0,
  "max_risk_per_trade_pct": 2.0,
  "risk_reward_min": 1.5,
  "sl_required": true,
  "tp_required": false,
  "position_sizing_method": "fixed_fractional",

  "max_concurrent_trades": {
    "scalping": 3,
    "day_trading": 5,
    "swing": 8,
    "total": 12
  },

  "drawdown": {
    "daily_limit_pct": 5.0,
    "weekly_limit_pct": 10.0,
    "max_consecutive_losses": 5,
    "consecutive_loss_pause_hours": 4,
    "action": "pause"
  },

  "news_filter": {
    "enabled": true,
    "pause_minutes_before": 30,
    "pause_minutes_after": 15,
    "impact_filter": "high",
    "affect_modes": ["scalping", "day_trading"]
  },

  "session_filter": {
    "enabled": true,
    "enforce_for": ["forex", "stocks", "indices", "commodities"]
  }
}
```

---

### `config/strategies.json`

See [03-strategies.md](./03-strategies.md) for the full strategy config schema. Key configurable parameters per strategy:

| Strategy | Key Parameters |
|---|---|
| EMA Scalp | `ema_fast`, `ema_slow`, `ema_bias`, `rsi_period`, `rsi_min`, `rsi_max` |
| BB Squeeze | `bb_period`, `bb_std`, `roc_period`, `min_squeeze_bars` |
| VWAP Reversion | `sigma_entry`, `sigma_sl`, `rsi_period`, `stoch_k`, `stoch_d`, `stoch_smooth` |
| MACD EMA Trend | `ema_fast`, `ema_slow`, `ema_bias`, `macd_fast`, `macd_slow`, `macd_signal` |
| S/R Breakout | `lookback_bars`, `atr_period`, `rsi_period`, `retest_mode` |
| RSI Divergence | `rsi_period`, `session_window_hours`, `ema_bias_period` |
| EMA Trend Rider | `d1_ema_fast`, `d1_ema_slow`, `h4_adx_min`, `h1_ema_entry` |
| Fibonacci RSI | `fib_levels`, `rsi_zone_min`, `rsi_zone_max`, `candle_confirm` |
| Weekly Breakout | `atr_multiplier`, `macd_fast`, `macd_slow`, `adx_min` |

---

### `config/app.json`

```json
{
  "trading_mode": "paper",
  "accounts": {
    "paper": {
      "login": 0,
      "server": "XMGlobal-Demo",
      "comment": "XM Demo Account"
    },
    "live": {
      "login": 0,
      "server": "XMGlobal-MT5",
      "comment": "XM Live Account"
    }
  },
  "execution_mode": {
    "scalping": "manual",
    "day_trading": "manual",
    "swing": "manual"
  },
  "notifications": {
    "signal_alert": true,
    "trade_opened": true,
    "trade_closed": true,
    "sl_hit": true,
    "tp_hit": true,
    "drawdown_alert": true,
    "circuit_breaker_triggered": true,
    "news_pause": true
  },
  "ai": {
    "price_prediction_enabled": false,
    "confidence_filter_enabled": false,
    "confidence_threshold": 60,
    "rl_agent_enabled": false
  }
}
```

> AI features default to `false` — they are activated in Phase 6 and 7 once sufficient trade history exists for training.

---

## Capital Security Summary

The following protections are always active and cannot be disabled from the UI (code-level enforcement):

1. **SL on every trade** — `sl_required: true` is enforced in the order manager, not just config
2. **Daily drawdown circuit breaker** — checked before every new order
3. **Max position size cap** — `max_risk_per_trade_pct` is a hard code-level ceiling
4. **No trade without connection** — if MT5 connection drops, no new orders are placed; existing trades remain open with broker-level SL protecting them
5. **Lot size sanity check** — calculated lot is validated against broker's `min_lot` and `max_lot` before sending
6. **Duplicate order guard** — if a signal fires twice (e.g., due to reconnect), the second order is rejected if a position in that symbol/direction already exists

---

## Profitability Strategy

The system becomes more profitable over time through three mechanisms:

1. **Signal confidence filtering (Phase 6):** AI scores each signal. Initially all signals with R:R ≥ 1.5 pass. After 100+ trades, low-confidence signals (score < `confidence_threshold`) are automatically skipped — quality over quantity.

2. **Reinforcement learning adaptation (Phase 7):** The RL agent observes which strategy + symbol + session + market condition combinations produce the best outcomes. It adjusts strategy weighting (not lot sizes or SL) to favor historically profitable combinations.

3. **Trade history analytics:** Win rate, profit factor, average R:R, and drawdown are tracked per strategy, per symbol, per session, and per market condition (trending/ranging). This data is visible in the dashboard and used to tune parameters.
