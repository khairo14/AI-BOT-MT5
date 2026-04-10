# AI-BOT-MT5 — Pricing & Valuation Reference

> Current date of assessment: March 2026  
> Bot status: Paper-trading active, live-capable, production branch deployed

---

## 1. What Is Being Valued

This document covers the full sales/licensing value of the AI-BOT-MT5 system, which includes:

| Component | Description |
| --- | --- |
| 9 live trading strategies | 3 scalping (M1/M5), 3 day-trading (M15/H1), 3 swing (H1/H4) |
| MT5 integration layer | Full order execution, paper & live modes, EA bridge for low-latency scalping |
| ML stack | LSTM models per symbol/strategy, RL agent (Q-learning), regime classifier, signal scorer |
| Risk engine | Circuit breaker, daily drawdown limit, per-symbol/mode concurrent limits, news filter, session filter |
| Parameter optimizer | Bayesian-style grid search with auto-trigger on win-rate degradation |
| Trade memory | Outcome recording, pattern-based scoring input per symbol/session/regime |
| FastAPI backend | 13 route groups, WebSocket signal bus, paper-trade sync loop |
| Next.js dashboard | 11 pages: live signals, analytics, backtest, ML status, settings, notifications, guide |

---

## 2. Market Channels & Price Ranges

### 2.1 MQL5 Marketplace (Compiled EA only)

Selling the compiled `.ex5` file as a standard retail EA.  
The ML/Python backend is not included — only the MT5 execution layer.

| Tier | Price | Notes |
| --- | --- | --- |
| Base entry | $200–$300 | Single-strategy EA, no ML |
| Mid tier | $400–$600 | Multi-strategy + basic risk rules |
| Premium (ML-backed) | $600–$900 | With documented backtest results |

**Limitation:** MQL5 buyers cannot inspect AI layers. Win-rate proof via myfxbook/verified statement is required to justify premium pricing.

---

### 2.2 Freelance / Custom Development (Upwork, Fiverr)

Selling as a commissioned custom bot to a retail trader or small fund.

| Scope | Price Range | Notes |
| --- | --- | --- |
| Backend only (Python + MT5) | $2,000–$4,000 | Strategy engine, risk manager, API |
| Full stack with dashboard | $5,000–$9,000 | All of the above + Next.js UI |
| Full stack + ML models | $8,000–$15,000 | Includes LSTM per-symbol + RL agent |

---

### 2.3 White-Label / SaaS License (B2B — prop firms, small funds)

Licensing the full source code + architecture to an institution.

| Package | Price Range | Includes |
| --- | --- | --- |
| Standard license | $15,000–$25,000 | Full source, docs, 1 month support |
| Enterprise license | $25,000–$50,000 | Source + models + ML pipeline + 3 month support |
| Ongoing SaaS (per seat/month) | $50–$200/month | Cloud-hosted, UI access, no source |

---

### 2.4 IP / Asset Sale (one-time full transfer)

Selling the complete codebase, models, and documentation outright.

| Condition | Price Range |
| --- | --- |
| Paper-trading only (no live track record) | $8,000–$20,000 |
| 3 months live with documented P&L (breakeven) | $20,000–$40,000 |
| 6+ months live with positive expectancy verified | $40,000–$70,000 |

---

## 3. Key Value Drivers

The following factors move the price **up**:

| Driver | Impact |
| --- | --- |
| Verified live track record (myfxbook/broker statement) | +30–50% to any figure above |
| Positive expectancy (avg RR > 1.5, win rate > 45%) | +20–30% |
| RL agent convergence (100+ Q-states per mode) | +10–20% |
| Multi-asset coverage (forex + crypto + indices + commodities) | +15–25% vs single-asset bots |
| Regime-aware parameter optimization running live | +10% |
| Clean audit trail (trade journal, backtest history, docs) | +5–10% |

The following factors move the price **down**:

| Detractor | Impact |
| --- | --- |
| No live verified results (paper only) | −40–50% |
| RL agent early stage (< 20 Q-states) | −10% |
| Strategies underperforming in current regime | −10–20% |

---

## 4. Competitor Benchmarks (2025–2026)

| Product | Price | What It Does |
| --- | --- | --- |
| Average MQL5 premium EA | $300–$500 | Single indicator, fixed rules, no ML |
| Capitalise.ai (SaaS subscription) | $79–$149/mo | NLP rule builder, no ML predictions |
| 3Commas (crypto) | $37–$99/mo | Grid/DCA bots, no regime awareness |
| Custom Quantconnect strategy | $5,000–$20,000 | Backtested quant strategy, no UI |
| Typical Upwork full-stack trading bot | $3,000–$10,000 | Custom EA + basic dashboard |

**This system is differentiated by:** LSTM + RL + regime classifier + signal scorer + full dashboard — a combination not found in any retail EA marketplace product.

---

## 5. Recommended Pricing Strategy

### Phase 1 — Now (paper trading, < 100 RL states)

- **Target:** Freelance/custom commissions
- **Price:** $6,000–$10,000 per client
- **Pitch:** "Full-stack algorithmic trading system with ML signal scoring and adaptive risk management"

### Phase 2 — After 3 months live with documented results

- **Target:** White-label to prop firms / small hedge funds
- **Price:** $20,000–$35,000 license fee + optional monthly support retainer
- **Add:** myfxbook link, monthly P&L screenshots, drawdown chart

### Phase 3 — After 6+ months with proven positive expectancy

- **Target:** IP/asset sale or institutional SaaS
- **Price:** $40,000–$70,000 outright, or $150–$200/seat/month SaaS
- **Requirement:** Independent audit of live results + formal documentation

---

## 6. What Is Needed Before Pricing Jumps

| Action | Estimated time | Price impact |
| --- | --- | --- |
| Run live with real money (even small lot) for 3 months | 3 months | +$15,000–25,000 to valuation |
| Reach 100+ RL Q-states per trading mode | 100–150 trades per mode | Demonstrates self-adaptation |
| Re-run parameter optimizer after trade data accumulates | Automatic after 20+ trades | Tighter strategy edge |
| Add myfxbook or broker-verified account link | 1 day setup | Credibility multiplier |
