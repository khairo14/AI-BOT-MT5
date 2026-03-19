# EVOTRADE-AI — Symbol Scope & Trading Category Map

## Guiding Principles

1. **Spread cost fits the timeframe** — scalping demands the tightest spreads. Day trading tolerates moderate spreads. Swing trading makes spread nearly irrelevant.
2. **Cash instruments only** — no futures/forwards with expiry dates to avoid rollover complexity.
3. **Session-aware** — stock CFDs only active during exchange hours. The bot enforces this automatically.
4. **Excluded categories** — Turbo Stocks (knock-out barrier risk), expiring futures, agricultural commodities, low-liquidity altcoins.
5. **Configurable** — the defaults below are bot-managed via `config/symbols.json`. Any symbol can be added/removed from the dashboard.

---

## Full Symbol Map

### FOREX

| Symbol | Full Name | Scalping | Day Trading | Swing | Rationale |
|---|---|---|---|---|---|
| EURUSD | Euro vs US Dollar | ✅ | ✅ | ✅ | Tightest spread globally, highest liquidity |
| GBPUSD | Great Britain Pound vs USD | ✅ | ✅ | ✅ | High volatility, strong intraday and weekly moves |
| USDJPY | US Dollar vs Japanese Yen | ✅ | ✅ | ✅ | 2nd most liquid pair, clean technical levels |
| EURJPY | Euro vs Japanese Yen | ✅ | ✅ | ❌ | Strong momentum, good for M1–H1; less clean on D1 |
| USDCHF | US Dollar vs Swiss Franc | ✅ | ❌ | ❌ | Low spread, good scalp pair; not recommended for day/swing |
| AUDUSD | Australian Dollar vs USD | ❌ | ✅ | ✅ | Asia session commodity link, clean trends |
| USDCAD | US Dollar vs Canadian Dollar | ❌ | ❌ | ✅ | Oil-correlated, strong weekly swing setups |
| GBPJPY | GBP vs Japanese Yen | ❌ | ✅ | ❌ | High range intraday, London/Tokyo overlap |
| NZDUSD | New Zealand Dollar vs USD | ❌ | ❌ | ✅ | Commodity currency, slow clean swing trends |

> All other forex crosses (AUDCAD, CHFJPY, EURNZD, GBPCAD, etc.) are **available for manual trading** but excluded from automated strategies by default due to wider spreads and thinner liquidity.

---

### EQUITY INDICES (Cash only)

| Symbol | Full Name | Scalping | Day Trading | Swing | Rationale |
|---|---|---|---|---|---|
| US100Cash | US 100 Index Cash | ✅ | ✅ | ✅ | Most traded index globally, clean technical levels |
| US30Cash | Wall Street 30 Cash | ✅ | ✅ | ✅ | High liquidity, strong intraday trends |
| US500Cash | US 500 Index Cash | ❌ | ✅ | ✅ | Broad market gauge, excellent for swing trends |
| GER40Cash | Germany 40 Index Cash | ❌ | ✅ | ✅ | European session leader, strong day/swing setups |
| UK100Cash | UK 100 Index Cash | ❌ | ✅ | ❌ | Good London session vehicle |

> Futures variants (US100-JUN26, US30-MAR26, etc.) are **excluded** — they expire and require rollover logic. Cash instruments roll automatically at the broker level.

---

### COMMODITIES (Cash only)

| Symbol | Full Name | Scalping | Day Trading | Swing | Rationale |
|---|---|---|---|---|---|
| GOLD | Gold Spot | ❌ | ✅ | ✅ | Most popular day-trade and swing instrument globally |
| SILVER | Silver Spot | ❌ | ❌ | ✅ | Follows Gold with more volatility, wide spread makes day-trade difficult |
| OilCash | WTI Oil Cash | ❌ | ✅ | ✅ | Strong intraday trends, macro-driven weekly swings |
| BRENTCash | Brent Crude Oil Cash | ❌ | ❌ | ✅ | Correlated with OilCash, better for multi-week positions |
| NGASCash | Natural Gas Cash | ❌ | ❌ | ✅ | Macro and seasonal driven, swing only |
| XPTUSD | Platinum Spot | ❌ | ❌ | ✅ | Optional, correlated to Gold/Silver |

> Agricultural futures (CORN, WHEAT, SUGAR, COCOA, etc.) are **excluded** — seasonal patterns, thin liquidity, expiry dates.

---

### STOCK CFDs

> Only active during exchange hours. US stocks: **14:30–21:00 UTC** (NYSE/NASDAQ).
> The bot automatically suppresses signals outside market hours for these instruments.

| Symbol | Full Name | Day Trading | Swing | Rationale |
|---|---|---|---|---|
| TSLA.OQ | Tesla Inc | ✅ | ✅ | Highest retail volume tech stock, strong intraday swings |
| NVDA.OQ | NVIDIA Corp | ✅ | ✅ | AI sector leader, strong momentum trends |
| AAPL.OQ | Apple Inc | ✅ | ✅ | Most stable mega-cap, reliable technical levels |
| MSFT.OQ | Microsoft Corp | ✅ | ✅ | Reliable trend instrument, AI exposure |
| AMZN.OQ | Amazon Inc | ✅ | ✅ | High range, institutional interest |
| GOOGL.OQ | Alphabet Inc | ❌ | ✅ | Better for swing, wider intraday spread |
| META.OQ | Meta Platforms | ❌ | ✅ | Strong weekly trend, swing preferred |
| NFLX.OQ | Netflix Inc | ❌ | ✅ | High swing range, earnings-driven moves |
| AMD.OQ | Advanced Micro Devices | ❌ | ✅ | Follows NVDA, add for diversification |

> Not suitable for scalping — spreads on stock CFDs are too wide for profitable M1/M5 strategies.

---

### CRYPTOCURRENCIES

> 24/7 market — no session restrictions. Spread is the main constraint; altcoins below top 5 have excessive spreads for systematic strategies.

| Symbol | Full Name | Day Trading | Swing | Rationale |
|---|---|---|---|---|
| BTCUSD | Bitcoin vs USD | ✅ | ✅ | Most liquid crypto, lowest spread, institutional grade |
| ETHUSD | Ethereum vs USD | ✅ | ✅ | 2nd most liquid, strong correlation/divergence trades from BTC |
| XRPUSD | Ripple vs USD | ❌ | ✅ | Volatile swing plays, not suited for intraday systematic |
| SOLUSD | Solana vs USD | ❌ | ✅ | High-growth asset, clean swing structure |
| ADAUSD | Cardano vs USD | ❌ | ✅ | Optional, longer swing cycles |

> All other cryptos (DOGE, ENJ, ZEC, DASH, FIL, MATIC, SHIB, etc.) are **excluded** by default — excessive spreads, high manipulation risk, no institutional support in XM's offering.

---

## Consolidated Active Symbol List by Trading Type

### Scalping (M1–M5) — 7 symbols

```
EURUSD, GBPUSD, USDJPY, EURJPY, USDCHF, US100Cash, US30Cash
```

### Day Trading (M15–H1) — 17 symbols

```
EURUSD, GBPUSD, USDJPY, AUDUSD, GBPJPY,
GOLD, OilCash,
US100Cash, US30Cash, US500Cash, GER40Cash, UK100Cash,
BTCUSD, ETHUSD,
TSLA.OQ, NVDA.OQ, AAPL.OQ, MSFT.OQ, AMZN.OQ
```

### Swing Trading (H4–D1) — 19 symbols

```
EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD,
GOLD, SILVER, OilCash, BRENTCash, NGASCash,
US100Cash, US500Cash,
BTCUSD, ETHUSD, XRPUSD, SOLUSD,
TSLA.OQ, NVDA.OQ, GOOGL.OQ, META.OQ, NFLX.OQ, AMD.OQ
```

---

## Configuration File

Symbol activation is controlled by `config/symbols.json`. Each symbol has an `enabled` flag per trading type. You can toggle symbols from the dashboard Settings panel without touching code.

```json
{
  "scalping": [
    { "symbol": "EURUSD",    "enabled": true },
    { "symbol": "GBPUSD",    "enabled": true },
    { "symbol": "USDJPY",    "enabled": true },
    { "symbol": "EURJPY",    "enabled": true },
    { "symbol": "USDCHF",    "enabled": true },
    { "symbol": "US100Cash", "enabled": true },
    { "symbol": "US30Cash",  "enabled": true }
  ],
  "day_trading": [
    { "symbol": "EURUSD",    "enabled": true },
    { "symbol": "GBPUSD",    "enabled": true },
    { "symbol": "USDJPY",    "enabled": true },
    { "symbol": "AUDUSD",    "enabled": true },
    { "symbol": "GBPJPY",    "enabled": true },
    { "symbol": "GOLD",      "enabled": true },
    { "symbol": "OilCash",   "enabled": true },
    { "symbol": "US100Cash", "enabled": true },
    { "symbol": "US30Cash",  "enabled": true },
    { "symbol": "US500Cash", "enabled": true },
    { "symbol": "GER40Cash", "enabled": true },
    { "symbol": "UK100Cash", "enabled": true },
    { "symbol": "BTCUSD",    "enabled": true },
    { "symbol": "ETHUSD",    "enabled": true },
    { "symbol": "TSLA.OQ",   "enabled": true },
    { "symbol": "NVDA.OQ",   "enabled": true },
    { "symbol": "AAPL.OQ",   "enabled": true },
    { "symbol": "MSFT.OQ",   "enabled": true },
    { "symbol": "AMZN.OQ",   "enabled": true }
  ],
  "swing": [
    { "symbol": "EURUSD",    "enabled": true },
    { "symbol": "GBPUSD",    "enabled": true },
    { "symbol": "USDJPY",    "enabled": true },
    { "symbol": "AUDUSD",    "enabled": true },
    { "symbol": "USDCAD",    "enabled": true },
    { "symbol": "NZDUSD",    "enabled": true },
    { "symbol": "GOLD",      "enabled": true },
    { "symbol": "SILVER",    "enabled": true },
    { "symbol": "OilCash",   "enabled": true },
    { "symbol": "BRENTCash", "enabled": true },
    { "symbol": "NGASCash",  "enabled": true },
    { "symbol": "US100Cash", "enabled": true },
    { "symbol": "US500Cash", "enabled": true },
    { "symbol": "BTCUSD",    "enabled": true },
    { "symbol": "ETHUSD",    "enabled": true },
    { "symbol": "XRPUSD",    "enabled": true },
    { "symbol": "SOLUSD",    "enabled": true },
    { "symbol": "TSLA.OQ",   "enabled": true },
    { "symbol": "NVDA.OQ",   "enabled": true },
    { "symbol": "GOOGL.OQ",  "enabled": true },
    { "symbol": "META.OQ",   "enabled": true },
    { "symbol": "NFLX.OQ",   "enabled": true },
    { "symbol": "AMD.OQ",    "enabled": true }
  ]
}
```

---

## Excluded Categories Reference

| Category | Reason |
|---|---|
| Turbo Stocks | Knock-out barrier — position wiped if price touches barrier. Incompatible with standard SL logic |
| Expiring Futures (US100-JUN26, OIL-MAY26, etc.) | Require rollover management; Cash equivalents are available |
| Agricultural Commodities (CORN, WHEAT, SUGAR, COCOA, etc.) | Seasonal patterns, thin intraday liquidity, expiry dates |
| Exotic Forex Crosses | Wide spreads make systematic strategies unprofitable |
| Low-cap Altcoins (DOGE, SHIB, ENJ, MATIC, etc.) | Excessive spread, market manipulation risk |
