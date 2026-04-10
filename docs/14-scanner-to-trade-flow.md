# Market Scanner to Trade Execution - Complete Flow

**Last Updated:** April 10, 2026  
**Purpose:** Explain end-to-end flow from scanner discovering symbols → placing trades

---

## Overview

The market scanner transforms the system from **passive** (only trading hardcoded symbols) to **active** (hunting for best opportunities across 100+ symbols).

**Current Problem:**
- 27 hardcoded symbols → only 3-5 signals/day
- Missing 90% of market opportunities
- Can't adapt when EURUSD goes flat but NZDUSD is pumping

**Scanner Solution:**
- Scans broker's full catalog (100+ symbols)
- Ranks by volatility, spread, trend strength, regime
- Auto-discovers hot symbols
- User adds high-scoring symbols with one click
- System generates signals for new symbols automatically

---

## 🔄 Complete Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│  PHASE 1: DISCOVERY (Every 15 minutes)                          │
└─────────────────────────────────────────────────────────────────┘

1. Scheduler Trigger (cron: 00,15,30,45 * * * *)
   ↓
2. Market Scanner Launches
   ├─ Fetches all available symbols from MT5 broker
   │  └─ XM: ~150 symbols (forex, commodities, indices, crypto, stocks)
   ├─ Filters: visible=true, select=true, tradeable=true
   └─ Result: ~100-120 tradeable symbols
   ↓
3. For each symbol, calculate opportunity score:
   ├─ Fetch OHLCV bars (200 bars for scalping/day, 100 for swing)
   ├─ Calculate metrics:
   │  ├─ ATR (Average True Range) → Volatility
   │  ├─ ADX (Average Directional Index) → Trend strength
   │  ├─ Spread cost (ask-bid spread as % of price)
   │  ├─ Volume (broker tick volume)
   │  ├─ Regime classification (trending/ranging/breakout)
   │  └─ Time since last news event
   └─ Score: 0-100 based on criteria match
   ↓
4. Rank all symbols by score (descending)
   ↓
5. Filter by threshold (score >= 70 for "good opportunity")
   ↓
6. Return top 20 results per trading type
   └─ Store in database: scanner_results table

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 2: USER REVIEW & SELECTION (Dashboard)                   │
└─────────────────────────────────────────────────────────────────┘

7. User opens Scanner Dashboard (/scanner page)
   ↓
8. Dashboard displays top opportunities:
   ┌──────────────────────────────────────────────────────────┐
   │ SCALPING - Top Opportunities                             │
   │ ┌────────────────────────────────────────────────────┐  │
   │ │ NZDUSD        Score: 87.5    [Add Symbol +]        │  │
   │ │ ✓ High volatility (0.82% ATR)                      │  │
   │ │ ✓ Tight spread (1.5 pips)                          │  │
   │ │ ✓ Ranging regime (mean-reversion setup)            │  │
   │ │ Metrics: ADX 18 | Volume 15k | Last update: 2m ago │  │
   │ └────────────────────────────────────────────────────┘  │
   │ ┌────────────────────────────────────────────────────┐  │
   │ │ GBPJPY        Score: 84.2    [Add Symbol +]        │  │
   │ │ ✓ Breakout pattern detected                        │  │
   │ │ ✓ Good volume (12k)                                │  │
   │ └────────────────────────────────────────────────────┘  │
   └──────────────────────────────────────────────────────────┘
   ↓
9. User clicks "Add Symbol +"
   ↓
10. System adds symbol to user_symbols table:
    ├─ user_id: current_user
    ├─ symbol: "NZDUSD"
    ├─ asset_class: "forex"
    ├─ enabled: true
    ├─ added_by: "scanner_auto"
    ├─ discovery_score: 87.5
    └─ discovery_reason: "High volatility, ranging regime"
    ↓
11. System creates default strategy_selection entries:
    ├─ NZDUSD + scalping (if score was for scalping)
    └─ OR all 3 trading types (if user chooses)
    ↓
12. UI updates: Symbol now in "My Active Symbols" list

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 3: SIGNAL GENERATION (Real-time loop)                    │
└─────────────────────────────────────────────────────────────────┘

13. Signal Bus Main Loop (runs every 1-5 minutes per trading type)
    ├─ Scalping: every 1 min
    ├─ Day trading: every 3 min
    └─ Swing: every 5 min
    ↓
14. Fetch user's enabled symbols from database:
    └─ Query: SELECT symbol FROM user_symbols 
              WHERE user_id=X AND enabled=true
    ↓
15. For EACH enabled symbol (including scanner-discovered ones):
    ├─ Check if LSTM model exists for this symbol
    │  ├─ If YES: Load model and predict
    │  └─ If NO: Skip LSTM, use strategy signals only
    ├─ Fetch latest OHLCV bars
    ├─ Calculate indicators (EMA, RSI, MACD, etc.)
    ├─ Run strategy logic (e.g., ema_scalp, bb_squeeze)
    ├─ Check regime classifier → Current regime?
    ├─ Get LSTM prediction (if model exists)
    ├─ Calculate signal confidence (LSTM + strategy + regime)
    └─ Decide: BUY / SELL / NONE
    ↓
16. If signal confidence >= threshold (0.65):
    ├─ Create StrategySignal object:
    │  ├─ symbol: "NZDUSD"
    │  ├─ direction: "BUY"
    │  ├─ trading_type: "scalping"
    │  ├─ confidence: 0.78
    │  ├─ entry_price: 1.0850
    │  ├─ sl_price: 1.0835 (calculated by strategy)
    │  ├─ tp_price: 1.0880 (calculated by strategy)
    │  └─ reason: "EMA crossover + LSTM bullish + ranging regime"
    └─ Queue signal for risk checks

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 4: RISK VALIDATION (Pre-execution)                       │
└─────────────────────────────────────────────────────────────────┘

17. Risk Manager validates signal:
    ├─ Check circuit breaker status
    │  ├─ Daily loss limit reached? → REJECT
    │  ├─ Weekly loss limit reached? → REJECT
    │  └─ Consecutive losses >= 3? → REJECT (cooldown)
    ├─ Check concurrent position limits
    │  ├─ Max positions per trading type? → REJECT if exceeded
    │  ├─ Max positions per symbol? → REJECT if symbol already open
    │  └─ Max total positions? → REJECT if exceeded
    ├─ Check correlation guard
    │  ├─ Get open positions in same asset class
    │  └─ If 2+ correlated positions exist → REJECT
    ├─ Check news filter
    │  ├─ High-impact news in next 15 min? → REJECT
    │  └─ News just happened? → REJECT (wait 15 min)
    ├─ Check session filter
    │  ├─ Market closed? → REJECT
    │  └─ Low-liquidity hours? → REJECT
    ├─ Check spread gate
    │  └─ Spread > 3x normal? → REJECT
    └─ Calculate position size (risk-based)
       ├─ Account balance: $10,000
       ├─ Risk per trade: 1.5% = $150
       ├─ SL distance: 15 pips
       ├─ Pip value: $10 per lot (NZDUSD)
       └─ Lot size = $150 / (15 pips × $10) = 1.0 lots
    ↓
18. If ALL checks PASS:
    └─ Approve signal for execution
    ↓
    If ANY check FAILS:
    ├─ Log rejection reason
    ├─ Send notification to user (optional)
    └─ STOP (signal discarded)

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 5: EXECUTION ROUTING (Manual vs Auto)                    │
└─────────────────────────────────────────────────────────────────┘

19. Check execution mode for this symbol+trading_type:
    ├─ Query: user_strategy_selection.execution_mode
    └─ Result: "manual" or "auto"
    ↓
    ┌─────────────────┐                ┌─────────────────┐
    │  MANUAL MODE    │                │   AUTO MODE     │
    └─────────────────┘                └─────────────────┘
    ↓                                   ↓
20a. Manual: Send signal to dashboard  20b. Auto: Execute immediately
    ├─ WebSocket broadcast:                ↓
    │  {                                21. Route to execution engine:
    │    "signal_id": "abc123",             ├─ If scalping → EA Bridge
    │    "symbol": "NZDUSD",                │  └─ Send to MQL5 EA (fast path)
    │    "direction": "BUY",                └─ If day/swing → Python API
    │    "confidence": 0.78,                   └─ OrderManager.place_market_order()
    │    "entry": 1.0850,                   ↓
    │    "sl": 1.0835,                  22. Place order on MT5:
    │    "tp": 1.0880                       ├─ direction: BUY_MARKET
    │  }                                    ├─ symbol: NZDUSD
    ├─ User sees notification               ├─ volume: 1.0 lots
    ├─ User clicks "Execute"                ├─ sl: 1.0835
    └─ Goes to step 21 →                    ├─ tp: 1.0880
                                            ├─ magic: 20241001
                                            ├─ comment: "scalp|NZDUSD|ema_scalp"
                                            └─ deviation: 20 points
                                            ↓
                                        23. MT5 Broker processes order:
                                            ├─ Check margin requirement
                                            ├─ Check symbol availability
                                            ├─ Match with counterparty
                                            └─ Fill order
                                            ↓
                                        24. Order result returned:
                                            ├─ Success: ticket=123456, fill_price=1.0851
                                            └─ Failure: error="insufficient margin"

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 6: POSITION TRACKING (Live monitoring)                   │
└─────────────────────────────────────────────────────────────────┘

25. If order succeeded:
    ↓
26. Create PaperPosition (if demo account) or LivePosition:
    ├─ ticket: 123456
    ├─ symbol: NZDUSD
    ├─ direction: BUY
    ├─ lot_size: 1.0
    ├─ open_price: 1.0851 (actual fill, not entry_price)
    ├─ sl_price: 1.0835
    ├─ tp_price: 1.0880
    ├─ open_time: 2026-04-10T14:35:22Z
    ├─ trading_type: scalping
    ├─ confidence: 0.78
    └─ status: "open"
    ↓
27. Save to database (positions table):
    └─ Store all position metadata
    ↓
28. Log to trade journal:
    └─ Write "OPEN" event to trade_journal.jsonl
    ↓
29. Start position monitoring thread:
    ├─ Poll MT5 position status every 1 second (scalping)
    ├─ OR every 5 seconds (day trading)
    ├─ OR every 30 seconds (swing)
    └─ Check if: TP hit, SL hit, manually closed, or modified

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 7: POSITION CLOSE (Outcome recorded)                     │
└─────────────────────────────────────────────────────────────────┘

30. Position reaches exit condition:
    ├─ TP hit: price reaches 1.0880 → CLOSE at profit
    ├─ SL hit: price drops to 1.0835 → CLOSE at loss
    ├─ Manual close: user clicks "Close" → CLOSE at market
    └─ Trailing stop: SL moved, then hit → CLOSE
    ↓
31. MT5 executes close:
    └─ Close price: 1.0880 (TP hit example)
    ↓
32. Calculate P&L:
    ├─ Entry: 1.0851
    ├─ Close: 1.0880
    ├─ Pips: (1.0880 - 1.0851) × 10000 = 29 pips
    ├─ Profit: 29 pips × 1.0 lots × $10/pip = $290
    ├─ Commission: -$7
    ├─ Swap: $0 (same-day close)
    └─ Net P&L: +$283
    ↓
33. Update position record:
    ├─ exit_price: 1.0880
    ├─ exit_time: 2026-04-10T14:42:15Z
    ├─ profit_loss: +283
    ├─ profit_pips: +29
    ├─ exit_reason: "tp_hit"
    ├─ duration_mins: 6.88 (scalping hold time)
    └─ status: "closed"
    ↓
34. Log to trade journal:
    └─ Write "CLOSE" event to trade_journal.jsonl
    ↓
35. Update RL Q-Table:
    ├─ State: (regime=ranging, confidence=high, adx=low)
    ├─ Action: BUY
    ├─ Reward: +0.0283 (2.83% profit)
    └─ Update Q-value using Q-learning formula
    ↓
36. Add to trade memory:
    └─ Store outcome for pattern analysis
    ↓
37. Update circuit breaker state:
    ├─ Reset consecutive_losses counter (if was losing streak)
    ├─ Update daily_pnl: +$283
    └─ Check if thresholds exceeded for next signal
    ↓
38. Update risk manager balance:
    └─ New account balance: $10,283
    ↓
39. Send WebSocket notification to dashboard:
    {
      "type": "position_closed",
      "ticket": 123456,
      "symbol": "NZDUSD",
      "profit": +283,
      "reason": "tp_hit",
      "duration": "6m 53s"
    }
    ↓
40. Check auto-retrain triggers:
    ├─ If 8 consecutive losses → Trigger LSTM retrain
    ├─ If 20 trades since last train → Trigger LSTM retrain
    └─ If win rate <45% + 30 trades → Trigger param optimizer

┌─────────────────────────────────────────────────────────────────┐
│  PHASE 8: ANALYTICS & LEARNING (Background)                     │
└─────────────────────────────────────────────────────────────────┘

41. Scanner re-evaluates symbol (next 15-min cycle):
    ├─ NZDUSD score drops to 65 (volatility decreased)
    └─ New top symbol: GBPJPY (score 89)
    ↓
42. User sees updated scanner results:
    └─ Optional: Auto-add GBPJPY if score >85 (user preference)
    ↓
43. Symbol performance tracking:
    ├─ NZDUSD: 5 trades, 80% win rate, +$450 total
    └─ Display in analytics dashboard
    ↓
44. System learns:
    ├─ RL agent: NZDUSD ranging regime → BUY action = good reward
    ├─ Scanner: Symbols with score >85 tend to be profitable
    └─ Optimizer: Tighter stops work better for ranging regimes

```

---

## 📊 Data Flow Summary

**User Symbols Database:**
```sql
-- Initially: 27 hardcoded symbols
SELECT * FROM user_symbols WHERE user_id=1;

symbol   | asset_class | enabled | added_by       | discovery_score
---------|-------------|---------|----------------|----------------
EURUSD   | forex       | true    | system_default | NULL
GBPUSD   | forex       | true    | system_default | NULL
GOLD     | commodities | true    | system_default | NULL
...

-- After scanner discovers NZDUSD:
NZDUSD   | forex       | true    | scanner_auto   | 87.5
GBPJPY   | forex       | true    | scanner_auto   | 84.2
```

**Signal Generation Loop:**
```python
# Before scanner: Only 27 symbols checked
for symbol in ["EURUSD", "GBPUSD", ...]:  # Hardcoded
    signal = generate_signal(symbol, trading_type)

# After scanner: Dynamic symbol list (100+ potential)
user_symbols = db.get_user_symbols(user_id, enabled=True)
for symbol in user_symbols:  # 27 default + X scanner-added
    signal = generate_signal(symbol.name, trading_type)
```

**Execution Flow:**
```
Signal Generated
    ↓
Risk Checks (10+ validation rules)
    ↓ PASS
Execution Mode Check
    ├─ Manual → Dashboard notification → User approves → Execute
    └─ Auto → Execute immediately
    ↓
MT5 Order Placement
    ↓
Position Tracking (1-30 second polls)
    ↓
Exit Triggered (TP/SL/manual)
    ↓
P&L Calculation
    ↓
Learning Updates (RL Q-table, trade memory)
    ↓
Scanner Re-evaluates (next cycle)
```

---

## 🎯 Key Decision Points

### Decision 1: Add Symbol or Not?
**Trigger:** Scanner shows NZDUSD with 87.5 score  
**User Choice:**
- ✅ Add → Signal generation starts for NZDUSD
- ❌ Skip → NZDUSD ignored, rescanned next cycle

### Decision 2: Execute Signal or Not?
**Trigger:** Signal confidence 0.78 (above 0.65 threshold)  
**System Choice:**
- **Manual mode:** Wait for user approval
- **Auto mode:** Execute immediately (if risk checks pass)

### Decision 3: Close Position or Hold?
**Trigger:** Position in profit, TP 5 pips away  
**System Behavior:**
- TP hit → Auto-close
- Manual close → User decides
- Trailing stop → System adjusts SL automatically

---

## 🔄 Scanner Impact - Before vs After

| Metric | Before Scanner | After Scanner | Improvement |
|--------|---------------|---------------|-------------|
| **Symbols monitored** | 27 (hardcoded) | 50-100 (dynamic) | 3-4x |
| **Signals per day** | 3-5 | 15-25 | 5x |
| **Adaptability** | None | Real-time | ∞ |
| **Opportunity cost** | High (missed NZDUSD pump) | Low (scanner finds it) | High |
| **User effort** | None (automated) | 1 click to add symbol | Minimal |
| **LSTM coverage** | Only 27 symbols | Gradual expansion | Growing |

---

## 🚨 Important Notes

### LSTM Model Availability
**Scanner discovers NZDUSD (new symbol), but no LSTM model exists yet:**
- ✅ System still works: Uses strategy signals only (EMA, RSI, MACD)
- ✅ Confidence score lower (0.60-0.65 vs 0.75-0.85 with LSTM)
- ⚠️ LSTM training triggered after 20 trades on NZDUSD
- ✅ After training: LSTM predictions improve signal quality

**Gradual LSTM expansion:**
```
Week 1: 27 LSTM models (hardcoded symbols)
Week 2: 32 LSTM models (scanner added 5 symbols with 20+ trades)
Week 3: 40 LSTM models (more scanner symbols accumulated data)
Week 4: 50+ LSTM models (system now covers top opportunities)
```

### Risk Validation is MANDATORY
**Even scanner-discovered symbols must pass all risk checks:**
- Circuit breakers (daily loss, consecutive losses)
- Position limits (per symbol, per type, total)
- Correlation guard (no excessive exposure to correlated assets)
- News filter (avoid high-impact events)
- Session filter (only trade during liquid hours)
- Spread gate (reject if spread too wide)

**Example rejection:**
```
Scanner: "GBPJPY score 84.2, add symbol!"
User: "Added GBPJPY"
Signal Bus: "GBPJPY BUY signal, confidence 0.72"
Risk Manager: "REJECT - Already have 2 JPY crosses open (correlation)"
→ Signal discarded, no trade placed
```

### Auto-Add Possibility (Future)
**Current flow:** Manual user approval required  
**Future enhancement:** Auto-add symbols with score >85
```python
# config/scanner.json
{
  "auto_add_enabled": true,
  "auto_add_threshold": 85,
  "auto_add_max_per_day": 3,
  "auto_enable_trading": false  # Add but keep disabled until user reviews
}
```

---

## 💡 Example End-to-End Scenario

**Time: 14:00 UTC**
1. Scanner runs (scheduled every 15 minutes)
2. Ranks 142 symbols from XM broker
3. Top result: NZDUSD (87.5 score)
   - Reason: High volatility (0.82% ATR), tight spread (1.5 pips), ranging regime

**Time: 14:02 UTC**
4. User opens scanner dashboard
5. Sees NZDUSD at top of scalping opportunities
6. Clicks "Add Symbol +"
7. NZDUSD added to enabled symbols list

**Time: 14:05 UTC**
8. Signal bus runs (scalping cycle, every 1 minute)
9. Checks NZDUSD (now in enabled list)
10. Calculates: EMA crossover + ranging regime detected
11. No LSTM model yet (new symbol) → Uses strategy signals only
12. Confidence: 0.68 (above 0.65 threshold)
13. Signal: BUY NZDUSD at 1.0850, SL 1.0835, TP 1.0880

**Time: 14:05:10 UTC**
14. Risk manager validates:
    - ✅ Circuit breaker: OK (no consecutive losses)
    - ✅ Position limits: OK (2/3 scalping positions)
    - ✅ Correlation: OK (no other NZD positions)
    - ✅ News filter: OK (no high-impact events)
    - ✅ Session: OK (London/NY overlap)
    - ✅ Spread: OK (1.5 pips < 3 pips limit)
15. Calculate lot size: 1.0 lots (based on $150 risk, 15 pip SL)
16. Execution mode: Manual → Send to dashboard

**Time: 14:05:15 UTC**
17. User sees notification: "NZDUSD BUY signal (confidence 68%)"
18. User reviews signal, clicks "Execute"
19. OrderManager.place_market_order() called
20. MT5 order placed: BUY 1.0 NZDUSD at market
21. Fill price: 1.0851 (1 pip slippage)

**Time: 14:05:20 UTC**
22. Position created: ticket 123456, open at 1.0851
23. Monitoring thread starts (polls every 1 second)

**Time: 14:12:15 UTC**
24. Price reaches 1.0880 (TP hit)
25. MT5 auto-closes position
26. P&L: +$283 (29 pips × $10/pip - $7 commission)
27. Update RL Q-table: ranging + BUY action = +0.0283 reward
28. Dashboard notification: "NZDUSD closed: +$283"

**Time: 14:15 UTC**
29. Scanner runs again
30. NZDUSD score drops to 65 (volatility decreased post-spike)
31. New top symbol: GBPJPY (score 89)
32. User adds GBPJPY, cycle repeats...

---

## 🔧 Technical Components Modified

### New Files Created:
- `engine/market_scanner.py` - Core scanning logic
- `api/routes/scanner.py` - REST API endpoints
- `config/scanner.json` - Criteria configuration
- `dashboard/app/scanner/page.tsx` - Scanner UI

### Existing Files Modified:
- `api/signal_bus.py` - Load user_symbols dynamically (not hardcoded)
- `config/symbols.json` - Becomes template/defaults only
- Database - Add `user_symbols` table

### No Changes Needed:
- `engine/risk_manager.py` - Already validates any symbol
- `engine/order_manager.py` - Already places orders for any symbol
- `ai/predictor.py` - Already handles missing models gracefully
- `ai/rl_agent.py` - Already updates Q-table for any symbol

---

**This flow transforms a static 27-symbol system into a dynamic opportunity hunter that adapts in real-time to market conditions.**
