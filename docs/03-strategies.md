# EVOTRADE-AI — Trading Strategies

## Strategy Design Rules

Every strategy in this system follows the same contract:

- **`signal()`** — evaluates indicators and returns BUY / SELL / NONE
- **`entry()`** — calculates entry price (market or limit)
- **`exit()`** — calculates SL and TP levels
- **`risk()`** — returns lot size based on account balance and risk %

All 9 strategies are implemented as Python classes inheriting from `BaseStrategy`. Configuration (period lengths, thresholds) lives in `config/strategies.json` and can be adjusted from the dashboard without code changes.

---

## SCALPING STRATEGIES
**Timeframes:** M1, M2, M5
**Session:** London (07:00–11:00 UTC) and New York (13:00–17:00 UTC) overlaps preferred
**Symbols:** EURUSD, GBPUSD, USDJPY, EURJPY, USDCHF, US100Cash, US30Cash
**Execution:** MQL5 EA (sub-millisecond order placement inside MT5 terminal)
**Target per trade:** 5–15 pips / equivalent index points
**Hold time:** Seconds to minutes

---

### S1 — EMA Scalp (Trend Following)

**Concept:** Two fast EMAs cross in the direction of a higher-timeframe trend bias. Trade only with the trend, never against it.

**Indicators:**
- EMA 8 and EMA 21 (on M1/M2)
- EMA 50 on M5 (trend bias filter)
- RSI 7 (momentum confirmation — avoid overbought/oversold entries)

**Signal Logic:**
- BUY: EMA 8 crosses above EMA 21, AND price is above EMA 50 (M5), AND RSI is between 40–65
- SELL: EMA 8 crosses below EMA 21, AND price is below EMA 50 (M5), AND RSI is between 35–60

**Entry:** Market order on confirmed close of the crossover candle

**Exit:**
- SL: 3 pips below/above the last swing low/high (pre-calculated per tick size)
- TP: 2:1 risk:reward ratio (TP = 2 × SL distance)

**Filters:**
- No trade within 15 minutes of high-impact news
- Spread must be below 1.5 pips (EURUSD baseline) at entry time

**Best symbols:** EURUSD, USDJPY, US100Cash

---

### S2 — Bollinger Band Squeeze Breakout

**Concept:** When volatility compresses (BB squeeze), a breakout in either direction follows. Enter the breakout with momentum confirmation.

**Indicators:**
- Bollinger Bands (20, 2.0) on M2/M5
- BB Width (to detect squeeze — when width drops below 20-period average BB width)
- Momentum indicator or Rate of Change (ROC 5)
- Volume (relative — current vs. 20-bar MA, for indices only)

**Signal Logic:**
- Detect squeeze: BB Width < 20-period average of BB Width
- BUY signal: price closes above the upper band after a squeeze AND ROC > 0
- SELL signal: price closes below the lower band after a squeeze AND ROC < 0

**Entry:** Market order on breakout candle close

**Exit:**
- SL: Midline of BB (moving average) at time of entry
- TP: Upper/lower band ± 1× the current BB Width

**Filters:**
- Squeeze must have lasted at least 5 bars (avoid false squeezes)
- No trade if BB Width is expanding rapidly already (chasing)

**Best symbols:** GBPUSD, US30Cash, US100Cash (higher volatility breakouts)

---

### S3 — VWAP Reversion

**Concept:** Price deviates from the Volume Weighted Average Price (VWAP) intraday and then reverts. Sell the deviation, buy the return. Works in ranging/slightly trending intraday conditions.

**Indicators:**
- VWAP (daily reset at 00:00 UTC)
- Standard deviation bands: VWAP ± 1σ, ± 2σ
- RSI 14 on M5 (confirm exhaustion)
- Stochastic (5,3,3) on M1 (entry timing)

**Signal Logic:**
- BUY: Price touches or closes below VWAP − 1.5σ, RSI < 35, Stochastic %K crosses above %D
- SELL: Price touches or closes above VWAP + 1.5σ, RSI > 65, Stochastic %K crosses below %D

**Entry:** Limit order at VWAP ± 1σ level (enter slightly inside deviation)

**Exit:**
- TP: VWAP midline
- SL: VWAP ± 2.5σ (one standard deviation beyond entry)

**Filters:**
- Only valid during established trading sessions (not during dead hours 21:00–00:00 UTC)
- Not valid on strong trend days — if price is on the same side of VWAP for 2+ hours, skip

**Best symbols:** EURUSD, GBPUSD, EURJPY, US100Cash

> Note: VWAP is not natively available in MT5 by default. A custom VWAP indicator will be added to the MT5 terminal and computed server-side in Python.

---

## DAY TRADING STRATEGIES
**Timeframes:** M15, M30, H1
**Session:** Active during relevant market sessions per symbol (Forex: London/NY; Indices: respective exchange hours; Crypto: any)
**Symbols:** Full day trading symbol list (17 instruments)
**Execution:** Python via `MetaTrader5` library
**Target per trade:** 30–100 pips / 0.5–2% price move
**Hold time:** Hours (closed before end of session)

---

### D1 — MACD + EMA Trend Following

**Concept:** Trade in the direction of the prevailing trend confirmed by both MACD momentum and EMA alignment. The cleanest and most reliable day-trade setup.

**Indicators:**
- EMA 20, EMA 50, EMA 200 on H1 (primary) and M15 (entry)
- MACD (12, 26, 9) on H1

**Signal Logic:**
- **BUY conditions (all must be true):**
  1. Price is above EMA 200 on H1 (macro bullish)
  2. EMA 20 > EMA 50 on H1 (trending up)
  3. MACD histogram is positive and increasing on H1
  4. On M15: price pulls back to EMA 20 and bounces with a bullish candle
- **SELL conditions (all must be true):**
  1. Price is below EMA 200 on H1 (macro bearish)
  2. EMA 20 < EMA 50 on H1 (trending down)
  3. MACD histogram is negative and decreasing on H1
  4. On M15: price pulls back to EMA 20 and rejects with a bearish candle

**Entry:** Market order on M15 candle close confirming the pullback rejection

**Exit:**
- SL: Below/above the recent swing low/high (M15), minimum 20 pips
- TP1: 1:1 risk:reward (close 50% of position)
- TP2: 1:2 risk:reward (close remaining 50%)

**Trailing Stop:** Activated after TP1 is hit — trail at EMA 20 (H1)

**Best symbols:** EURUSD, GBPUSD, GOLD, US100Cash, US30Cash, TSLA, NVDA

---

### D2 — Support / Resistance Breakout

**Concept:** Price breaks through a significant S/R level with volume confirmation. Ride the momentum after the breakout retest.

**Indicators:**
- Auto-detected S/R levels (fractal-based, looking back 50 bars on H1)
- ATR 14 on H1 (to set SL and filter false breakouts by volatility)
- RSI 14 (breakout momentum — RSI should be above 55 for bullish, below 45 for bearish)
- Candlestick close as confirmation (no wicks-through — the candle must close beyond the level)

**Signal Logic:**
- BUY: H1 candle closes above a significant resistance level, RSI > 55, ATR is above its 14-period average (volatile environment)
- SELL: H1 candle closes below a significant support level, RSI < 45, ATR above average
- **Retest entry (preferred):** After the breakout close, wait for price to retest the broken level. Enter when the retest candle closes and holds.

**Entry:** Limit order at the broken S/R level during retest, or market order on breakout close (configurable)

**Exit:**
- SL: Previous S/R level (the one just broken) + 10% ATR buffer
- TP: Next significant S/R level (auto-detected)

**Best symbols:** GOLD, OilCash, US500Cash, BTCUSD, ETHUSD

---

### D3 — RSI Divergence + Session Open

**Concept:** RSI divergence (price makes new high/low but RSI does not) signals trend exhaustion. Combined with the high-energy session open, this creates precise reversal entries.

**Indicators:**
- RSI 14 on M30/H1
- Price action divergence detection (higher-high on price, lower-high on RSI = bearish divergence; vice versa = bullish)
- Session open time filter (London: 07:00 UTC, New York: 13:00 UTC)
- EMA 50 on H1 (trend context — only take divergence trades that go toward the mean)

**Signal Logic:**
- **Bearish divergence BUY reversal:** price makes lower low, RSI makes higher low → bullish divergence → BUY
- **Bullish divergence SELL reversal:** price makes higher high, RSI makes lower high → bearish divergence → SELL
- Both signals require the divergence to form within 2 hours of a session open
- Additional confirmation: RSI crossing 50 in the direction of the trade

**Entry:** Market order on the RSI 50 cross, or limit at the most recent swing

**Exit:**
- SL: Beyond the divergence extreme (the low/high that formed the divergence)
- TP: 1:1.5 risk:reward initially, move to breakeven after 1:1

**Best symbols:** GBPUSD, EURJPY, GOLD, GBPJPY, GER40Cash

---

## SWING TRADING STRATEGIES
**Timeframes:** H4, D1
**Session:** No session restrictions (position held until TP/SL, days to weeks)
**Symbols:** Full swing symbol list (19+ instruments)
**Execution:** Python via `MetaTrader5` library
**Target per trade:** 100–500+ pips / 2–10% price move
**Hold time:** Days to weeks

---

### W1 — Multi-Timeframe EMA Trend Rider

**Concept:** Align trend direction across three timeframes (D1, H4, H1), then enter on a pullback. The higher the timeframe confluence, the stronger the trade.

**Indicators:**
- EMA 50 and EMA 200 on D1 (macro trend direction)
- EMA 21 and EMA 50 on H4 (intermediate trend)
- EMA 8 and EMA 21 on H1 (entry timing)
- ADX 14 on H4 (trend strength — only trade when ADX > 25)

**Signal Logic:**
- **BUY conditions:**
  1. Price above EMA 200 on D1
  2. EMA 50 > EMA 21 slope is upward on H4
  3. ADX > 25 on H4
  4. On H1: EMA 8 crosses above EMA 21 after a pullback to EMA 21
- **SELL conditions:** Mirror of above (price below EMA 200 D1, etc.)

**Entry:** Limit order at H1 EMA 21 level during pullback phase

**Exit:**
- SL: Below/above H4 EMA 50 (strong support in uptrend)
- TP1: Previous H4 swing high/low (partial close 40%)
- TP2: D1 swing high/low (close remaining 60%)
- Trailing: Move SL to breakeven after TP1 hit

**Best symbols:** EURUSD, GOLD, US100Cash, BTCUSD, TSLA, NVDA

---

### W2 — Fibonacci Retracement + RSI Confluence

**Concept:** After a significant impulsive move, price retraces to key Fibonacci levels. Enter when RSI confirms momentum exhaustion at the Fib level.

**Indicators:**
- Fibonacci retracement tool (auto-applied to detected impulse swings on H4/D1)
- Key levels: 38.2%, 50%, 61.8% (golden zone: 50%–61.8%)
- RSI 14 on H4
- Candlestick patterns at Fib level (pin bar, engulfing — confirmation)

**Signal Logic:**
- Detect an impulse move on H4 (5+ candles, ATR multiple move)
- Auto-plot Fibonacci from swing low to high (bullish) or high to low (bearish)
- BUY: Price retraces to 50%–61.8% zone, RSI is between 35–50 (not oversold yet), bullish reversal candle forms
- SELL: Price retraces to 50%–61.8% zone, RSI is between 50–65, bearish reversal candle forms

**Entry:** Limit order at 61.8% level (deepest of the golden zone)

**Exit:**
- SL: Below/above the 78.6% level (structure invalidation)
- TP1: 38.2% of the impulse (initial relief)
- TP2: 0% (full retracement back to impulse start) or next major level
- Risk:Reward target: minimum 1:2

**Best symbols:** GOLD, SILVER, OilCash, BTCUSD, ETHUSD, XRPUSD, SOLUSD

---

### W3 — Weekly High/Low Breakout with ATR Filter

**Concept:** The prior week's high and low are major psychological and technical levels. Breakout beyond these levels at the start of a new week with momentum often leads to large sustained moves.

**Indicators:**
- Previous week high and low (auto-plotted on D1/H4)
- ATR 14 on D1 (volatility filter — breakout must exceed 0.5× ATR to be valid)
- MACD (12, 26, 9) on D1 (trend confirmation)
- ADX 14 on D1 (strength filter — ADX > 20 required)

**Signal Logic:**
- Monitor on Monday–Tuesday for breakout of prior week's range
- BUY: D1 candle closes above prior week high, MACD histogram positive, ADX > 20, close is at least 0.5× ATR above the weekly high
- SELL: D1 candle closes below prior week low, MACD histogram negative, ADX > 20

**Entry:** Stop order placed 10 pips beyond the weekly high/low (catches the breakout in motion)

**Exit:**
- SL: Back inside the weekly range (just below the broken weekly high / above weekly low)
- TP: 1.5× the prior week's range projected from the breakout point
- Weekly structure check: re-evaluate every Monday

**Filters:**
- Skip if the current week opens with a gap beyond the level (chasing an already-broken gap)
- Skip if ATR is abnormally high (news week — FOMC, NFP, CPI — confirm before entering)

**Best symbols:** EURUSD, GBPUSD, USDCAD, NGASCash, BRENTCash, META, NFLX, AMD

---

## Strategy Selection Config

```json
{
  "scalping": {
    "active_strategies": ["ema_scalp", "bb_squeeze", "vwap_reversion"],
    "symbol_strategy_override": {
      "US100Cash": ["bb_squeeze", "vwap_reversion"],
      "USDCHF":    ["ema_scalp"]
    }
  },
  "day_trading": {
    "active_strategies": ["macd_ema_trend", "sr_breakout", "rsi_divergence"],
    "symbol_strategy_override": {
      "BTCUSD":    ["sr_breakout"],
      "TSLA.OQ":   ["macd_ema_trend", "sr_breakout"]
    }
  },
  "swing": {
    "active_strategies": ["ema_trend_rider", "fibonacci_rsi", "weekly_breakout"],
    "symbol_strategy_override": {
      "BTCUSD":    ["fibonacci_rsi", "weekly_breakout"],
      "NGASCash":  ["weekly_breakout"]
    }
  }
}
```

---

## Strategy Performance Tracking

Each strategy records per-trade:
- Symbol, direction, entry price, SL, TP, time
- Exit reason: TP1, TP2, SL, manual close, trailing stop
- P&L in pips and account currency
- AI confidence score at signal time
- Strategy name + trading type

This feeds the ML training loop and the dashboard's per-strategy performance stats panel.
