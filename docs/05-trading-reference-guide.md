# Trading Reference Guide

> This guide is embedded in the EVOTRADE-AI dashboard as a quick-reference panel. It covers the essentials you need to understand what the bot is doing and why — no prior professional trading knowledge required.

---

## What to Trade — Eligibility Quick Reference

Use this as a fast reminder when choosing symbols or reviewing bot settings.

### Scalping — Best Instruments

> Rule: Only trade instruments with the tightest spreads and highest liquidity. Every pip of spread is a direct cost on a 5–15 pip target.

| Symbol | Why It's Good for Scalping |
|---|---|
| **EURUSD** | Tightest spread of any instrument. Moves predictably on M1. #1 scalp pair |
| **GBPUSD** | Slightly wider spread but high volatility — great 5–15 pip moves on M1/M5 |
| **USDJPY** | Second most liquid pair. Clean technical levels on short timeframes |
| **EURJPY** | Strong intraday momentum, especially London session |
| **USDCHF** | Low spread, inversely correlated to EURUSD — good for diversification |
| **US100Cash** | High liquidity index, strong M1 directional moves during NY open |
| **US30Cash** | Similar to US100, excellent during US session |

**Avoid for scalping:** Stocks (spread too wide), Crypto (volatility unpredictable on M1), Commodities (spread too wide), Exotic pairs

---

### Day Trading — Best Instruments

> Rule: Strong intraday range, active during your session, manageable spread for 30–100 pip targets.

| Symbol | Why It's Good for Day Trading | Best Session |
|---|---|---|
| **EURUSD** | Highest volume, reliable H1 trends | London + NY overlap |
| **GBPUSD** | Large daily range, strong trend days | London + NY |
| **USDJPY** | Clean H1 structure, reacts well to US data | Tokyo + NY |
| **AUDUSD** | Commodity-linked, clean Asia/London trends | Asia + London |
| **GBPJPY** | Highest daily range of major pairs — volatile but rewarding | London + NY |
| **GOLD** | Most popular day-trade globally. Huge intraday range | London + NY |
| **OilCash** | Strong directional moves, reactive to news | NY session |
| **US100Cash** | Trending or ranging — strategies work well on M15/H1 | NY session |
| **US30Cash** | Reliable intraday trends | NY session |
| **US500Cash** | Broad market, less whippy than US100 | NY session |
| **GER40Cash** | European index — strong London session moves | London session |
| **UK100Cash** | FTSE 100, good London vehicle | London session |
| **BTCUSD** | 24/7, strong intraday breakout and trend moves | Any |
| **ETHUSD** | Follows BTC with higher % moves | Any |
| **TSLA, NVDA, AAPL, MSFT, AMZN** | High-volume US stocks with strong intraday momentum | NY session only |

**Avoid for day trading:** Agricultural commodities, Turbo stocks, low-cap altcoins, expiring futures

---

### Swing Trading — Best Instruments

> Rule: Clean H4/D1 structure, macro-driven price action, holds for days–weeks. Spread is irrelevant. Position size is smaller. Focus on the big picture.

| Symbol | Why It's Good for Swing Trading | Typical Hold |
|---|---|---|
| **EURUSD** | Core trend pair. Weekly ranges of 100–300 pips | 3–10 days |
| **GBPUSD** | Wide weekly swings. Strong reactions to UK/US macro events | 3–14 days |
| **USDJPY** | Interest rate differential driven. Multi-week institutional flows | 5–20 days |
| **AUDUSD** | Commodity-currency, clean D1 trends | 5–14 days |
| **USDCAD** | Oil-correlated, reliable D1 structure | 5–14 days |
| **NZDUSD** | Slower, cleaner swing setup. Low noise on D1 | 1–3 weeks |
| **GOLD** | Safe-haven + inflation hedge. Multi-week macro moves | 1–4 weeks |
| **SILVER** | Follows Gold with amplified volatility | 1–3 weeks |
| **OilCash** | Supply/demand driven. Strong weekly trend potential | 1–3 weeks |
| **BRENTCash** | Correlated to OilCash, geopolitical moves | 1–3 weeks |
| **NGASCash** | Seasonal + macro moves, low correlation to other assets | 2–6 weeks |
| **US100Cash** | Tech sector trend. Multi-week bull/bear cycles | 1–4 weeks |
| **US500Cash** | Broad market swing. Less noise than US100 | 1–4 weeks |
| **BTCUSD** | Crypto cycles — large multi-week moves | 1–8 weeks |
| **ETHUSD** | Follows BTC, sometimes leads on alt seasons | 1–6 weeks |
| **XRPUSD, SOLUSD** | Higher-beta crypto swings | 1–4 weeks |
| **TSLA, NVDA, GOOGL, META, NFLX, AMD** | Earnings-driven + sector trend swings | 1–3 weeks |

**Avoid for swing trading:** Turbo stocks, expiring futures (roll risk), low-cap altcoins (manipulation)

---

### At a Glance — Symbol Eligibility Summary

| Symbol | Scalping | Day Trading | Swing |
|---|---|---|---|
| EURUSD | ✅ | ✅ | ✅ |
| GBPUSD | ✅ | ✅ | ✅ |
| USDJPY | ✅ | ✅ | ✅ |
| EURJPY | ✅ | ✅ | ❌ |
| USDCHF | ✅ | ❌ | ❌ |
| AUDUSD | ❌ | ✅ | ✅ |
| USDCAD | ❌ | ❌ | ✅ |
| GBPJPY | ❌ | ✅ | ❌ |
| NZDUSD | ❌ | ❌ | ✅ |
| GOLD | ❌ | ✅ | ✅ |
| SILVER | ❌ | ❌ | ✅ |
| OilCash | ❌ | ✅ | ✅ |
| BRENTCash | ❌ | ❌ | ✅ |
| NGASCash | ❌ | ❌ | ✅ |
| US100Cash | ✅ | ✅ | ✅ |
| US30Cash | ✅ | ✅ | ❌ |
| US500Cash | ❌ | ✅ | ✅ |
| GER40Cash | ❌ | ✅ | ✅ |
| UK100Cash | ❌ | ✅ | ❌ |
| BTCUSD | ❌ | ✅ | ✅ |
| ETHUSD | ❌ | ✅ | ✅ |
| XRPUSD | ❌ | ❌ | ✅ |
| SOLUSD | ❌ | ❌ | ✅ |
| TSLA, NVDA, AAPL, MSFT, AMZN | ❌ | ✅ | ✅ |
| GOOGL, META, NFLX, AMD | ❌ | ❌ | ✅ |
| Turbo Stocks | ❌ | ❌ | ❌ |
| Expiring Futures | ❌ | ❌ | ❌ |
| Agricultural Commodities | ❌ | ❌ | ❌ |
| Low-cap Altcoins | ❌ | ❌ | ❌ |

---

## Trailing Stop

A trailing stop is a stop-loss that **automatically moves in your favor as price moves with you**, locking in profit progressively.

**How it works:**
- You open a BUY at 1.1000 with SL at 1.0950 (50 pip risk)
- Price moves to 1.1060 — trailing stop moves SL up to 1.1010
- Price moves to 1.1100 — SL is now at 1.1050
- If price reverses to 1.1050, trade closes at +50 pips profit automatically

**Where trailing stops are used in this bot:**

| Mode | Strategy | When Activated |
|---|---|---|
| Day Trading | MACD EMA Trend (D1) | After TP1 is hit — remaining 50% of position trails at EMA 20 (H1) |
| Day Trading | RSI Divergence (D3) | Move SL to breakeven after 1:1 R:R is reached |
| Swing Trading | EMA Trend Rider (W1) | Move SL to breakeven after TP1, then trail at H4 EMA 50 |
| Swing Trading | Fibonacci RSI (W2) | Move SL to breakeven after TP1 hits |

**Scalping does NOT use trailing stops** — hold times are 10 seconds to 10 minutes. Fixed TP is used for precision and speed.

---

## Trading Types

### Scalping
- **What it is:** Opening and closing trades within seconds to a few minutes to capture small price moves.
- **Timeframes used:** M1 (1-minute), M2, M5
- **Typical target per trade:** 5–15 pips
- **Typical hold time:** 10 seconds – 10 minutes
- **Session:** Best during London (07:00–11:00 UTC) and New York (13:00–17:00 UTC) overlap. Never scalp in dead hours.
- **Key risk:** Spreads eat profit fast. Only trade the tightest spread symbols. Never scalp during news events.
- **Good for:** Fast, compounding small wins. High trade frequency.

### Day Trading
- **What it is:** Trades opened and closed within the same trading session. No position held overnight.
- **Timeframes used:** M15, M30, H1
- **Typical target per trade:** 30–100 pips / 0.5–2% price move
- **Typical hold time:** 1–8 hours
- **Session:** Active during relevant market sessions per symbol. US stocks only during US market hours.
- **Key risk:** Holding too long into low-liquidity periods. Being caught by session-close gap.
- **Good for:** Catching intraday trends. Balanced activity level.

### Swing Trading
- **What it is:** Trades held for days to weeks to capture larger price swings.
- **Timeframes used:** H4, D1
- **Typical target per trade:** 100–500+ pips / 2–10% move
- **Typical hold time:** 2 days – 4 weeks
- **Session:** No restriction. Positions held through weekends.
- **Key risk:** Weekend gap risk. Surprise macro events while holding. Requires patience.
- **Good for:** Lower stress, high reward-per-trade, works well alongside a job or other activity.

---

## Key Trading Terms

### Price & Orders

| Term | Meaning |
|---|---|
| **Bid** | The price the broker buys from you (you sell at Bid) |
| **Ask** | The price the broker sells to you (you buy at Ask) |
| **Spread** | Ask − Bid. This is the broker's fee. Tighter = better for short-term trades |
| **Pip** | Smallest standard price movement. For EUR/USD: 0.0001. For USD/JPY: 0.01 |
| **Lot** | Standard unit of trade size. 1 standard lot = 100,000 units of base currency |
| **Micro lot** | 0.01 lots = 1,000 units. Smallest typical retail trade size |
| **Market Order** | Buy/sell immediately at current market price |
| **Limit Order** | Set a specific price to buy/sell at — executes only when price reaches that level |
| **Stop Order** | Executes when price moves to a certain level in the opposite direction (used for breakouts) |

### Risk & Protection

| Term | Meaning |
|---|---|
| **Stop-Loss (SL)** | A price at which the trade automatically closes to limit your loss |
| **Take-Profit (TP)** | A price at which the trade automatically closes to lock in your profit |
| **Trailing Stop** | SL that moves with price as it goes in your favor — locks in more profit over time |
| **Drawdown** | The reduction in account value from a peak. A 5% drawdown means balance is 5% below its highest point |
| **Risk:Reward (R:R)** | Ratio of potential loss to potential gain. R:R of 1:2 means risk $50 to potentially make $100 |
| **Position Sizing** | Calculating how many lots to trade so you only risk a fixed % of your balance |
| **Leverage** | Broker allows you to control a larger position than your actual balance. XM offers up to 1:1000. Higher leverage = higher risk |
| **Margin** | The amount of your balance reserved as collateral for an open trade |
| **Margin Call** | Warning that your account balance is too low to support open positions — broker may close them forcibly |

### Performance Metrics

| Term | Meaning |
|---|---|
| **Win Rate** | % of trades that are profitable. A 50% win rate is fine with good R:R |
| **Profit Factor** | Total gross profit ÷ total gross loss. >1.5 is good. >2.0 is excellent |
| **Expectancy** | Average amount you expect to earn per trade: (Win Rate × Avg Win) − (Loss Rate × Avg Loss) |
| **Max Drawdown** | The worst peak-to-trough decline in account history. Key metric for capital safety |
| **Sharp Ratio** | Risk-adjusted return. Higher = better returns for the risk taken |

---

## Indicators Quick Reference

### Trend Indicators

| Indicator | What It Shows |
|---|---|
| **EMA (Exponential Moving Average)** | Smoothed average price weighted toward recent data. Used to identify trend direction. EMA 20 < EMA 50 = downtrend |
| **MACD** | Moving Average Convergence Divergence. Shows trend momentum and direction. Histogram above zero = bullish momentum |
| **ADX (Average Directional Index)** | Measures trend strength only (not direction). ADX > 25 = strong trend. ADX < 20 = ranging/choppy |

### Momentum / Oscillators

| Indicator | What It Shows |
|---|---|
| **RSI (Relative Strength Index)** | Momentum oscillator 0–100. Above 70 = overbought (may reverse down). Below 30 = oversold (may reverse up). 50 = neutral |
| **Stochastic** | Similar to RSI. Compares closing price to price range. Used for timing entries in ranging markets |
| **ROC (Rate of Change)** | Speed of price change. Positive = price accelerating up. Negative = accelerating down |

### Volatility

| Indicator | What It Shows |
|---|---|
| **Bollinger Bands (BB)** | 3 lines around price: middle (moving average) ± 2 standard deviations. Wide bands = high volatility. Narrow = low volatility (squeeze) |
| **ATR (Average True Range)** | Average size of candles over N periods. Used to set realistic SL distance and detect abnormal volatility |

### Volume & Price Level Tools

| Indicator | What It Shows |
|---|---|
| **VWAP (Volume Weighted Average Price)** | Average price weighted by volume, reset each day. Price above VWAP = bullish intraday bias. Used as a mean-reversion target |
| **Support Level** | A price zone where buying has repeatedly stopped price from falling further |
| **Resistance Level** | A price zone where selling has repeatedly stopped price from rising further |
| **Fibonacci Retracement** | Key levels (38.2%, 50%, 61.8%) measured from a swing move. Price often pauses or reverses at these levels |

---

## Chart Patterns Reference

### Reversal Patterns (signal end of a trend)

| Pattern | Description |
|---|---|
| **Pin Bar** | Candle with a long wick and small body. Wick points toward the rejected price direction. Strong reversal signal |
| **Engulfing** | A candle that completely covers the body of the previous candle. Bullish engulfing after downtrend = reversal signal |
| **Double Top / Bottom** | Price hits the same level twice and fails. Double top = potential reversal down. Double bottom = potential reversal up |
| **Head and Shoulders** | Three peaks — middle one highest. Classic reversal pattern indicating trend exhaustion |

### Continuation Patterns (signal trend resumes after pause)

| Pattern | Description |
|---|---|
| **Bull/Bear Flag** | Sharp move followed by a tight consolidation channel. Breakout from channel continues original move |
| **Ascending/Descending Triangle** | Horizontal resistance/support with converging trendline. Breakout in direction of trend |
| **Inside Bar** | A candle whose high and low are within the previous candle's range. Signals consolidation before a breakout |

---

## Market Sessions (UTC)

| Session | Open (UTC) | Close (UTC) | Key Pairs |
|---|---|---|---|
| Sydney | 22:00 | 07:00 | AUD pairs, NZD pairs |
| Tokyo (Asian) | 00:00 | 09:00 | JPY pairs, AUD pairs |
| London | 07:00 | 16:00 | EUR pairs, GBP pairs, Gold |
| New York | 13:00 | 21:00 | USD pairs, US indices, US stocks |
| **London–NY Overlap** | **13:00** | **16:00** | **Highest liquidity of the day** |

> Scalping is most effective during the London–New York overlap (13:00–16:00 UTC). This is when spreads are tightest, volume is highest, and price movement is most reliable.

---

## Risk Rules to Remember

1. **Never risk more than 1–2% per trade.** One loss should never noticeably damage your account.
2. **Always have a stop-loss.** The market can move against you instantly on news events.
3. **Risk:Reward minimum 1:1.5.** If you lose as often as you win, you still need positive expectancy.
4. **Don't move your SL wider under pressure.** If the trade is going wrong, respect the SL.
5. **Drawdown is normal — panic is not.** Every strategy goes through losing streaks. The circuit breaker handles it.
6. **Don't trade during major news without a filter.** The spread spikes dramatically and SL can be skipped (slippage).
7. **Demo first. Always.** Test any new strategy configuration on the paper account before switching to live.

---

## Symbols Glossary

| Symbol | Full Name | Category |
|---|---|---|
| EURUSD | Euro vs US Dollar | Forex Major |
| GBPUSD | Great Britain Pound vs US Dollar | Forex Major |
| USDJPY | US Dollar vs Japanese Yen | Forex Major |
| AUDUSD | Australian Dollar vs US Dollar | Forex Major |
| USDCAD | US Dollar vs Canadian Dollar | Forex Major |
| USDCHF | US Dollar vs Swiss Franc | Forex Major |
| EURJPY | Euro vs Japanese Yen | Forex Cross |
| GBPJPY | Great Britain Pound vs Japanese Yen | Forex Cross |
| NZDUSD | New Zealand Dollar vs US Dollar | Forex Major |
| GOLD / XAUUSD | Gold Spot vs US Dollar | Commodity |
| SILVER / XAGUSD | Silver Spot vs US Dollar | Commodity |
| OilCash | WTI Crude Oil Cash | Commodity |
| BRENTCash | Brent Crude Oil Cash | Commodity |
| NGASCash | Natural Gas Cash | Commodity |
| US100Cash | Nasdaq 100 Index Cash (NAS100) | Equity Index |
| US30Cash | Dow Jones 30 Index Cash (DJ30) | Equity Index |
| US500Cash | S&P 500 Index Cash | Equity Index |
| GER40Cash | Germany DAX 40 Index Cash | Equity Index |
| UK100Cash | FTSE 100 Index Cash | Equity Index |
| BTCUSD | Bitcoin vs US Dollar | Cryptocurrency |
| ETHUSD | Ethereum vs US Dollar | Cryptocurrency |
| XRPUSD | Ripple vs US Dollar | Cryptocurrency |
| SOLUSD | Solana vs US Dollar | Cryptocurrency |
| TSLA.OQ | Tesla Inc Stock CFD | Stock CFD |
| NVDA.OQ | NVIDIA Corp Stock CFD | Stock CFD |
| AAPL.OQ | Apple Inc Stock CFD | Stock CFD |
| MSFT.OQ | Microsoft Corp Stock CFD | Stock CFD |
| AMZN.OQ | Amazon Inc Stock CFD | Stock CFD |
| GOOGL.OQ | Alphabet (Google) Stock CFD | Stock CFD |
| META.OQ | Meta Platforms Stock CFD | Stock CFD |
| NFLX.OQ | Netflix Inc Stock CFD | Stock CFD |
| AMD.OQ | AMD (Advanced Micro Devices) Stock CFD | Stock CFD |
