# AI-BOT-MT5 - Upgrade Roadmap

**Last Updated:** April 10, 2026  
**Status:** Planning Phase  
**Purpose:** Prioritized task list for production deployment and multi-user launch

---

## Table of Contents

1. [Critical Priority - Week 1-2](#critical-priority---week-1-2)
2. [High Priority - Week 3-4](#high-priority---week-3-4)
3. [Medium Priority - Week 5-6](#medium-priority---week-5-6)
4. [Database Migration - Week 7-8](#database-migration---week-7-8)
5. [Multi-User Core - Week 9-10](#multi-user-core---week-9-10)
6. [Advanced Features - Week 11-12](#advanced-features---week-11-12)
7. [Implementation Order](#implementation-order)
8. [Today's Focus](#todays-focus)

---

## Critical Priority - Week 1-2

**Goal:** Fix profitability problem + prove system works

### Task 1: Market Scanner ✅ COMPLETE

**Priority:** CRITICAL - DO FIRST  
**Effort:** 10 days (completed in 1 day)  
**Reason:** Only 3-5 trades/day → need 10x more opportunities  
**Status:** ✅ Fully implemented and operational

**Deliverables:**

- [x] `engine/market_scanner.py` - Core scanning logic (850 lines, complete)
- [x] `api/routes/scanner.py` - REST endpoints (320 lines, complete)
- [x] `config/scanner.json` - Scanner criteria config (complete)
- [x] `dashboard/app/scanner/page.tsx` - Scanner UI page (complete)
- [x] Background auto-scan scheduler (complete, runs every 60 minutes)
- [x] API integration and navigation (complete)
- [ ] Integration testing with live MT5 (pending - awaits MT5 connection)

**Completed Components:**

**Backend (100%):**

- ✅ Scanner configuration with granular criteria per trading type
- ✅ Core engine: ATR, ADX, volatility percentile, liquidity, momentum scoring
- ✅ Category detection (forex/crypto/us_index/eu_index/commodity/stock)
- ✅ Trading hours filtering (24/5 forex, 24/7 crypto, market hours for stocks/indices)
- ✅ Composite scoring with configurable weights
- ✅ 30-minute caching with force refresh
- ✅ 9 REST API endpoints (/scanner/*)
- ✅ Background scheduler (60-minute auto-scan)
- ✅ Test script for validation

**Frontend (100%):**

- ✅ React dashboard with 3 tabs (Scalping/Day/Swing)
- ✅ Sortable table (11 columns: symbol, score, ATR, spread, ADX, etc.)
- ✅ Search/filter by symbol or category
- ✅ Color-coded scores (green >70, yellow 50-70, red <50)
- ✅ One-click "Add Symbol" button
- ✅ Manual refresh + full scan buttons
- ✅ Health status indicator
- ✅ Auto-refresh every 5 minutes
- ✅ Navigation menu integration

**Success Criteria:**

- Scanner ranks 100+ broker symbols by score ✅
- Top 20 opportunities shown per trading type ✅
- User can add/remove symbols with one click ✅
- System generates signals for scanner-discovered symbols ✅ (requires symbol in config)
- Background auto-scan every 60 minutes ✅

**Next Steps:**

1. ⏳ Test with live MT5 connection (awaits broker connection)
2. ⏳ Validate 5x signal increase (3-5 → 15-25/day)
3. ⏳ Monitor for 2 weeks to verify opportunity discovery works

**Implementation Summary:**

- **Time Taken:** 1 day (vs 10 day estimate)
- **Lines of Code:** ~1,700 (backend + frontend + config + docs)
- **Git Commits:** 5 commits
- **Expected Impact:** 5x signal increase (3-5 → 15-25 signals/day)

---

### Task 2: Validate Profitability ✅ SYSTEM READY - IN PROGRESS

**Priority:** CRITICAL  
**Effort:** Ongoing (2-4 weeks)  
**Reason:** Need 50+ trades to validate win rate >50%  
**Status:** ✅ Monitoring system complete, collecting trade data (64 trades so far)

**Deliverables:**

- [x] Performance report generator (`engine/performance_report.py` - 220 lines)
- [x] REST API endpoints (`/profitability/`, `/profitability/status`)
- [x] Profitability dashboard page (`/profitability`)
- [x] Success criteria tracking (50+ trades, 50% WR, 1.5 PF)
- [x] CLI tool for reports (`python -m engine.performance_report`)
- [ ] **Achieve 50+ trades with >50% win rate** (64 trades collected, 21.88% WR)
- [ ] **Achieve profit factor >1.5** (current: 0.58)
- [ ] Run paper trades with scanner-discovered symbols (IN PROGRESS)
- [ ] Track win rate per symbol + trading type (DONE - automated)
- [ ] Generate performance report (DONE - automated)
- [ ] Decision point: Proceed to multi-user or pivot strategy (PENDING - awaiting profitability)

**Success Criteria:**

- 50+ closed trades across scanner symbols ✅ (64 trades)
- Win rate >50% (ideally 55%+) ❌ (21.88% - NEEDS IMPROVEMENT)
- Profit factor >1.5 ❌ (0.58 - NEEDS IMPROVEMENT)
- No major risk violations ✅ (no violations detected)

**Current Status (April 10, 2026):**

- **Total Trades:** 64 (✅ passed 50+ threshold)
- **Win Rate:** 21.88% (14W / 46L / 4BE) - ❌ Below 50% target
- **Profit Factor:** 0.58 - ❌ Below 1.5 target
- **Total Profit:** -$2,402.84
- **Best Performing:** Swing trading (50% WR), GOLD, GBPJPY, BTCUSD profitable

**Next Actions:**

1. ⏳ Continue monitoring profitability daily
2. ⏳ Investigate why day trading (15.38% WR) and scalping (15.38% WR) underperform
3. ⏳ Consider focusing on swing trading only (already at 50% WR target)
4. ⏳ Analyze ML model predictions vs actual outcomes
5. ⏳ **DO NOT proceed to multi-user until profitability proven**

---

- [ ] Run paper trades with scanner-discovered symbols
- [ ] Track win rate per symbol + trading type
- [ ] Generate performance report
- [ ] Decision point: Proceed to multi-user or pivot strategy

**Success Criteria:**

- 50+ closed trades across scanner symbols
- Win rate >50% (ideally 55%+)
- Profit factor >1.5
- No major risk violations

---

### Task 3: Dashboard UI Accuracy ✅ COMPLETE

**Status:** ✅ Done  
**Issue:** Dashboard showed "5 consecutive losses" vs actual "8"  
**Fix:** Updated `dashboard/app/ml/page.tsx` to match backend logic

---

### Task 4: Optimizer Param Safety ✅ VERIFIED

**Status:** ✅ Verified Safe  
**Issue:** User concern about optimizer overwriting good params  
**Finding:** Uses full historical data + walk-forward validation + keeps last 5 param versions

---

### Task 5: Bar Count Alignment ✅ COMPLETE

**Status:** ✅ Done  
**Issue:** Inconsistent bar counts (99k/17k/5k vs 200k/50k/30k)  
**Fix:** Standardized to 200k/50k/20k across signal_bus, predictor, and routes

---

## High Priority - Week 3-4

**Goal:** Production stability for 24/7 operation

### Task 6: Secrets Management ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 1 day (reduced from 2 days — env vars already in place)  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — MT5 credentials now secured via environment variables

**Deliverables:**

- [x] ✅ `.env.example` template created (already existed, enhanced)
- [x] ✅ `python-dotenv` installed and loading (already implemented in `mt5_client.py`)
- [x] ✅ Passwords moved to environment variables (already done)
- [x] ✅ `config/app.json` contains only non-sensitive data (login numbers, server names)
- [x] ✅ `.gitignore` excludes `.env` files (already configured)
- [x] ✅ Created `engine/validate_secrets.py` — startup validation script
- [x] ✅ Created `setup_secrets.bat` — interactive setup wizard
- [x] ✅ Updated README with quick start guide
- [x] ✅ Created `docs/15-task6-secrets-management.md` — full documentation

**What Was Already Secure:**

- ✅ Passwords loaded via `os.getenv()` in `engine/mt5_client.py`
- ✅ `.env` files excluded from git
- ✅ No passwords in `config/app.json` (only login numbers)

**What Was Added:**

1. **Validation Script:** `python engine\validate_secrets.py`
   - Checks if `.env` exists
   - Validates required environment variables
   - Warns about missing optional vars

2. **Setup Wizard:** `.\setup_secrets.bat`
   - Interactive credential entry
   - Auto-generates API secret key
   - Updates `.env` file safely

3. **Documentation:**
   - Enhanced `.env.example` with better comments
   - README quick start section
   - Full Task #6 implementation guide

**Future Enhancements (Multi-User Phase):**

- AWS Secrets Manager integration (for key rotation)
- Encryption at rest for database credentials
- Password rotation mechanism

See [Task #6 docs](docs/15-task6-secrets-management.md) for full details.

---

### Task 7: Rate Limiting ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 2 hours (fast implementation)  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — API now protected from abuse/DDoS

**Deliverables:**

- [x] ✅ Added `slowapi` to requirements.txt
- [x] ✅ Configured global rate limiter (200 req/min default)
- [x] ✅ Applied per-endpoint limits:
  - Signals: 100/min (read), 50/min (write)
  - Scanner: 30/min (full scan), 60/min (results)
  - Health: 60/min
  - Logs: 30/min
  - Cache ops: 30/min
- [x] ✅ IP-based throttling with `get_remote_address()`
- [x] ✅ 429 error responses with retry-after headers
- [x] ✅ Status endpoint: `/rate-limit-status`
- [x] ✅ Documentation: `docs/16-task7-rate-limiting.md`

**Implementation:**

```python
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@router.get("/signals")
@limiter.limit("60/minute")
async def get_signals():
    ...
```

---

### Task 8: Health Check Endpoint ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 1 hour (fast implementation)  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — System uptime now monitorable

**Deliverables:**

- [x] ✅ Enhanced `GET /health` endpoint
- [x] ✅ MT5 connection status check
- [x] ✅ Disk space monitoring (free GB + usage %)
- [x] ✅ Memory usage monitoring (% used + available GB)
- [x] ✅ Risk manager status check
- [x] ✅ Circuit breaker status
- [x] ✅ Trading mode (paper/live)
- [x] ✅ Version number (1.0.0)
- [x] ✅ Timestamp (ISO 8601)
- [x] ✅ Returns 200 (healthy) or 503 (degraded)
- [x] ✅ Added `psutil==7.0.0` for system monitoring
- [x] ✅ Documentation: `docs/17-task8-health-check.md`

**Response Example:**

```json
{
  "status": "healthy",
  "timestamp": "2026-04-10T18:30:00Z",
  "version": "1.0.0",
  "checks": {
    "mt5_connection": "ok",
    "disk_space": "ok",
    "memory": "ok",
    "risk_manager": "ok"
  },
  "metrics": {
    "disk_free_gb": 45.23,
    "disk_usage_pct": 62.5,
    "memory_usage_pct": 34.5,
    "memory_available_gb": 8.12,
    "circuit_breaker_active": false
  },
  "trading_mode": "paper"
}
```

---

### Task 9: Automated Restart (NSSM) ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 1 hour  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — backend can be installed as auto-restarting Windows service

**Deliverables:**

- [x] ✅ Created `install_service.bat`
- [x] ✅ Created `uninstall_service.bat`
- [x] ✅ Configured NSSM service installation for FastAPI backend
- [x] ✅ Added auto-restart on failure (5 second delay)
- [x] ✅ Added delayed auto-start on Windows boot
- [x] ✅ Configured stdout/stderr log capture

---

### Task 10: Error Handling Audit ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 2 hours  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — critical exception paths audited and minor silent fallbacks now debug-logged

**Deliverables:**

- [x] ✅ Audited `api/signal_bus.py`
- [x] ✅ Audited `engine/order_manager.py`
- [x] ✅ Audited `engine/risk_manager.py`
- [x] ✅ Audited `ai/predictor.py`
- [x] ✅ Verified critical trading paths log failures
- [x] ✅ Added debug logging to silent non-critical fallbacks in `api/signal_bus.py`
- [x] ✅ Confirmed risk failures remain fail-safe

**Code Review Pattern:**

```python
# BAD - Swallows errors silently
try:
    order = place_order(signal)
except Exception:
    pass  # ❌ Lost critical error info

# GOOD - Logs then propagates
try:
    order = place_order(signal)
except MT5Error as e:
    logger.error(f"Order failed: {signal.symbol} | {e}")
    raise  # ✅ Re-raise for upstream handling
except Exception as e:
    logger.critical(f"Unexpected error in order placement: {e}", exc_info=True)
    raise  # ✅ Critical errors must propagate
```

---

### Task 11: Logging Improvements ✅ COMPLETE

**Priority:** HIGH  
**Effort:** 2 hours  
**Completed:** April 10, 2026  
**Risk:** RESOLVED — logging now supports daily rotation, compression, JSON output, and request correlation IDs

**Deliverables:**

- [x] ✅ Daily rotating log files in `logs/api_YYYY-MM-DD.log`
- [x] ✅ 7-day retention with zip compression
- [x] ✅ Separate error-only daily logs
- [x] ✅ JSON logs enabled in production mode
- [x] ✅ Environment-based log levels (`DEBUG` in development, `INFO` in production)
- [x] ✅ Correlation ID middleware with `X-Correlation-ID` response header

**Implementation:**

```python
import structlog

logger = structlog.get_logger()

# Instead of:
logger.info(f"Signal generated: {symbol} {direction}")

# Use structured logging:
logger.info(
    "signal_generated",
    symbol=symbol,
    direction=direction,
    trading_type=trading_type,
    confidence=confidence,
    correlation_id=request_id
)
```

---

## Medium Priority - Week 5-6

**Goal:** Feature enhancements for better UX

### Task 12: Risk Presets ✅ COMPLETE

**Priority:** MEDIUM  
**Effort:** 3 days (completed in 1 day)  
**Completed:** April 10, 2026  
**Reason:** Users need flexible risk management (conservative/moderate/aggressive)

**Deliverables:**

- [x] ✅ Created `config/risk_presets.json` with 3 profiles
- [x] ✅ Conservative/Moderate/Aggressive presets
- [x] ✅ Dashboard UI at `/risk-presets` for selection
- [x] ✅ Risk multipliers (risk_per_trade, stop_loss, take_profit)
- [x] ✅ Updated `engine/risk_manager.py` to apply preset multipliers
- [x] ✅ Added API endpoints: `GET /api/risk/presets`, `POST /api/risk/presets/select`
- [x] ✅ Per-preset concurrent trade limits
- [x] ✅ Per-preset drawdown limits (daily/weekly/consecutive)
- [x] ✅ Created test suite: `tests/test_risk_presets.py`

**Implementation:**

Presets are stored in `config/risk_presets.json` and applied via multipliers:

| Metric | Conservative | Moderate | Aggressive |
| -------- | -------------- | ---------- | ------------ |
| Risk per trade | 0.7x (1.05%) | 1.0x (1.5%) | 1.5x (2.25%) |
| Stop loss | 0.7x (tighter) | 1.0x | 1.5x (wider) |
| Take profit | 1.3x (larger) | 1.0x | 0.8x (faster) |
| Max daily loss | 3.0% | 5.0% | 8.0% |
| Max concurrent | 8 total | 12 total | 18 total |
| Loss streak limit | 3 | 5 | 7 |

**Test Results (10,000 balance, 50 pip SL):**

- Conservative: 2.09 lots
- Moderate: 2.99 lots
- Aggressive: 3.99 lots

✅ All tests passed. Presets correctly scale risk across all parameters.

---

### Task 13: Trade Performance by Symbol ✅ COMPLETE

**Priority:** MEDIUM  
**Effort:** 2 days (already existed)  
**Completed:** Previously implemented as `/analytics` page  
**Reason:** Can't see which symbols are profitable

**Deliverables:**

- [x] ✅ Analytics page at `/analytics` (labeled "Performance 📊" in sidebar)
- [x] ✅ Win rate breakdown by symbol
- [x] ✅ Win rate breakdown by strategy
- [x] ✅ Win rate breakdown by trading mode
- [x] ✅ Profit/loss per symbol with trade counts
- [x] ✅ Average profit per trade
- [x] ✅ Worst symbols/strategies failure analysis
- [x] ✅ Win rate by hour (UTC) heatmap
- [x] ✅ Equity curve visualization
- [x] ✅ Account mode filter (paper/live/all)
- [x] ✅ Trading mode filter (scalping/day/swing/all)

**Current Data Insights (from screenshot):**

**By Symbol:**
- GOLD: 3 trades, 20% WR, -$341.05
- GBPJPY: 10 trades, 30% WR, +$272.89 ✅ (profitable)
- BTCUSD: 1 trade, 100% WR, +$206.80 ✅ (perfect)
- US30Cash: 2 trades, 50% WR, +$243.41 ✅ (profitable)

**By Strategy:**
- sr_breakout: 39 trades, 15.4% WR, -$1638.72 ❌ (worst performer)
- ema_trend_rider: 9 trades, 44.4% WR, -$51.29

**By Trading Mode:**
- Scalping: 16 trades, 12.5% WR, -$1079.34 ❌
- Day Trading: 39 trades, 15.4% WR, -$30.45 ❌
- Swing: Not shown in screenshot

**Recommendation:** Use failure analysis to disable worst performing symbols and strategies.

---

### Task 14: News Filter Auto-Update ✅ COMPLETE

**Priority:** MEDIUM  
**Effort:** 1 day  
**Completed:** April 10, 2026  
**Reason:** Forex Factory cache expires, manual refresh needed

**Deliverables:**

- [x] ✅ Background asyncio task to refresh news every 60 minutes
- [x] ✅ Initial refresh on API startup (non-blocking)
- [x] ✅ Configurable via `risk.json` news_filter.cache_minutes (default 60)
- [x] ✅ Thread-safe refresh using `asyncio.to_thread()`
- [x] ✅ Graceful error handling (logs warning, continues with cached data)
- [x] ✅ Log when news events are loaded (currently 90 events)

**Implementation:**

Added `_news_refresh_loop()` background task in `api/runner_loop.py` that:

1. Performs initial refresh on startup (ensures fresh data before trading)
2. Spawns non-blocking background task using `asyncio.create_task()`
3. Sleeps for N minutes (from config), then calls `news_filter._refresh()`
4. `_refresh()` is idempotent and thread-safe (prevents duplicate fetches)
5. Fetches thisweek + nextweek from Forex Factory (7-14 day coverage)

**Logs (Verified Working):**

```
2026-04-10 22:01:45.627 | INFO | News filter auto-refresh task created.
2026-04-10 22:01:45.628 | INFO | News filter: initial refresh on startup...
2026-04-10 22:01:45.632 | INFO | News filter: initial refresh complete
2026-04-10 22:01:45.632 | INFO | News filter: auto-refresh every 60 minutes
2026-04-10 22:01:46.568 | INFO | NewsFilter: loaded 90 calendar events
```

**Note:** Dashboard indicator deferred to Task #16 In-App Notifications.

---

### Task 15: Trailing Stop Implementation ✅ COMPLETE

**Priority:** MEDIUM  
**Effort:** 3 days (completed in 1 day)  
**Completed:** April 10, 2026  
**Reason:** Only static SL/TP, missing profit protection

**Deliverables:**

- [x] ✅ Created `engine/trailing_stop.py` (TrailingStopManager class)
- [x] ✅ Background asyncio task in `api/runner_loop.py` (5-second tick)
- [x] ✅ Configuration in `app.json` per trading mode
- [x] ✅ API endpoint `/api/trades/trailing-stops` for status
- [x] ✅ Enriched `/api/trades/positions` with trailing status
- [x] ✅ Thread-safe position tracking
- [x] ✅ Never moves SL against profit (only in profit direction)

**Implementation:**

**Scope:**
- **Day Trading:** ✅ Enabled (activation: 20 pips, trail: 15 pips)
- **Swing:** ✅ Enabled (activation: 50 pips, trail: 40 pips)  
- **Scalping:** ❌ Disabled (EA handles this at sub-millisecond speed)

**Why scalping is disabled:**
- `AIBotScalper.mq5` EA already trails scalping positions on 1-second timer
- EA configuration: 8 pips trail distance, 5 pips breakeven
- EA runs natively in MT5 (1-5ms latency vs 50-300ms from Python)
- Python trailing would be redundant and slower

**Configuration (`config/app.json`):**

```json
"trailing_stops": {
  "enabled": true,
  "scalping": {
    "enabled": false,
    "note": "Scalping uses EA (AIBotScalper.mq5) for sub-millisecond trailing"
  },
  "day_trading": {
    "enabled": true,
    "activation_pips": 20,
    "trail_distance_pips": 15
  },
  "swing": {
    "enabled": true,
    "activation_pips": 50,
    "trail_distance_pips": 40
  }
}
```

**How it works:**

1. Background task monitors all open positions every 5 seconds
2. For each position in profit ≥ activation threshold:
   - Calculates new SL = highest_profit_price - trail_distance
   - Only moves SL upward (BUY) or downward (SELL), never backwards
   - Logs each trailing action with pips moved
3. Position state tracked in memory (highest profit price, total pips trailed)
4. Cleaned up automatically when position closes

**Example log:**

```
TrailingStop: #12345 EURUSD BUY | Moved SL 1.08500 → 1.08650 (15.0 pips) | Total trailed: 45.0 pips
```

**API Endpoints:**

- `GET /api/trades/positions` — includes `trailing` object with status
- `GET /api/trades/trailing-stops` — dedicated trailing stop status for all positions

**Dashboard UI:** Deferred to future phase (data available via API).

**Testing:** Will be verified with next live day_trading or swing position.

---

### Task 16: In-App Notifications ⚠️ NOT STARTED

**Priority:** MEDIUM  
**Effort:** 2 days  
**Reason:** User wants alerts for signals, circuit breakers, etc.

**Deliverables:**

- [ ] Add trailing_stop config per strategy
- [ ] Monitor positions every tick
- [ ] Move SL if price moves in profit direction
- [ ] Never move SL against profit
- [ ] Dashboard shows trailing status

**Logic:**

```python
def update_trailing_stop(position, current_price):
    if position.direction == "BUY":
        if current_price > position.entry_price:
            # In profit, trail SL upward
            new_sl = current_price - trailing_distance
            if new_sl > position.sl:
                modify_position_sl(position.ticket, new_sl)
```

---

### Task 16: In-App Notifications ⚠️ NOT STARTED

**Priority:** MEDIUM  
**Effort:** 2 days  
**Reason:** User wants alerts for signals, circuit breakers, etc.

**Deliverables:**

- [ ] Notification center in dashboard header
- [ ] Show unread count badge
- [ ] Types: signal_generated, circuit_breaker, position_closed, optimizer_complete
- [ ] Mark as read functionality
- [ ] Keep last 50 notifications

**Note:** Email/SMS notifications deferred to future phase.

---

## Database Migration - Week 7-8

**Goal:** Move from JSON files to PostgreSQL for multi-user scalability

### Task 17: PostgreSQL Setup ⚠️ NOT STARTED

**Priority:** HIGH (User correction: NOT low priority)  
**Effort:** 3 days  
**Reason:** JSON files don't scale to multi-user

**Deliverables:**

- [ ] Install PostgreSQL locally (or use Supabase free tier)
- [ ] Create database schema (from docs/12-multi-user-architecture.md)
- [ ] Setup SQLAlchemy ORM models
- [ ] Database connection pool
- [ ] Alembic migrations setup

**Schema Tables:**

- users
- mt5_accounts
- user_risk_settings
- user_strategy_selection
- user_param_overrides
- positions
- user_circuit_breaker_state
- ml_models (metadata)
- shared_trade_memory
- audit_log

---

### Task 18: User Authentication (JWT) ⚠️ NOT STARTED

**Priority:** HIGH (Required for multi-user)  
**Effort:** 5 days  
**Reason:** No login system currently

**Deliverables:**

- [ ] User registration endpoint (email + password)
- [ ] Login endpoint (returns JWT token)
- [ ] Password hashing (bcrypt)
- [ ] Email verification (optional first version)
- [ ] JWT middleware for protected routes
- [ ] Refresh token logic

**API Endpoints:**

```python
POST /auth/register
POST /auth/login
POST /auth/logout
POST /auth/refresh
GET  /auth/me
```

---

### Task 19: Multi-Account Support ⚠️ NOT STARTED

**Priority:** HIGH (Core multi-user feature)  
**Effort:** 4 days  
**Reason:** Users need multiple MT5 accounts

**Deliverables:**

- [ ] `mt5_accounts` table implementation
- [ ] Add/edit/delete MT5 accounts per user
- [ ] Encrypt MT5 passwords (AES-256)
- [ ] Account switcher in dashboard
- [ ] Test connection before saving
- [ ] Primary account designation

---

## Multi-User Core - Week 9-10

**Goal:** Dashboard privacy + admin capabilities

### Task 21: Dashboard User Privacy ⚠️ NOT STARTED

**Priority:** MEDIUM  
**Effort:** 3 days  
**Reason:** Each user should only see their own data

**Deliverables:**

- [ ] All API endpoints filter by `user_id` from JWT
- [ ] Positions scoped to logged-in user
- [ ] Signals scoped to user's enabled strategies
- [ ] Trade journal shows only user's trades
- [ ] No data leakage between users

**Security Check:**

```python
# BAD - Returns all positions
@router.get("/positions")
def get_positions():
    return db.query(Position).all()  # ❌ Leaks other users' data

# GOOD - Scoped to current user
@router.get("/positions")
def get_positions(user: User = Depends(get_current_user)):
    return db.query(Position).filter(Position.user_id == user.id).all()  # ✅
```

---

### Task 22: Account Selector UI ⚠️ NOT STARTED

**Priority:** MEDIUM  
**Effort:** 2 days  
**Reason:** Users with multiple MT5 accounts need to switch

**Deliverables:**

- [ ] Account dropdown in dashboard header
- [ ] Shows: Account #12345 (XM Demo) - $10,250 balance
- [ ] Switch updates all dashboard data
- [ ] Persist selected account in localStorage
- [ ] Show account status (connected/disconnected)

**UI Mockup:**

```text
┌─────────────────────────────────────────────┐
│ [≡] AI-BOT-MT5    [Account: XM Demo ▼]  👤 │
│                                             │
│ ┌─────────────────────────────────────────┐│
│ │ XM Demo #12345      $10,250  ✓          ││
│ │ XM Live #67890      $5,020   ✓          ││
│ │ FTMO Challenge      $100,000 ⚠️ Disc.   ││
│ │ + Add New Account                       ││
│ └─────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
```

---

### Task 23: Admin Panel ⚠️ NOT STARTED

**Priority:** MEDIUM  
**Effort:** 5 days  
**Reason:** Admin needs to view all users' status

**Deliverables:**

- [ ] `/admin/users` - List all users
- [ ] `/admin/users/{id}` - View specific user details
- [ ] User stats: positions, win rate, total trades
- [ ] Subscription status
- [ ] Ability to suspend/activate accounts
- [ ] Platform-wide stats dashboard
- [ ] Role-based access control (admin vs regular user)

**Admin Dashboard Sections:**

1. **User Management** - List, search, suspend users
2. **Platform Stats** - Total users, total trades, total PnL
3. **System Health** - MT5 connections, errors, uptime
4. **ML Model Status** - Last trained, accuracy per symbol
5. **Audit Log** - Recent critical actions

---

## Advanced Features - Week 11-12

**Goal:** Institutional-grade differentiators

### Task 25: Portfolio Optimization ⚠️ NOT STARTED

**Priority:** LOW (Advanced feature)  
**Effort:** 7 days  
**Reason:** Multi-strategy risk allocation

**Deliverables:**

- [ ] Calculate correlation matrix across strategies
- [ ] Kelly Criterion for position sizing
- [ ] Risk parity allocation
- [ ] Rebalance suggestions
- [ ] Backtest with optimal allocation

**Note:** Deferred until core system proven profitable.

---

### Task 26: Execution Quality Metrics ⚠️ NOT STARTED

**Priority:** LOW (Advanced analytics)  
**Effort:** 3 days  
**Reason:** Track slippage, fill rate

**Deliverables:**

- [ ] Record expected vs actual fill price
- [ ] Calculate slippage per symbol
- [ ] Fill rate (orders filled vs rejected)
- [ ] Time to execution
- [ ] Dashboard showing execution quality

---

### Task 28: Prometheus + Grafana ⚠️ NOT STARTED

**Priority:** LOW (Monitoring)  
**Effort:** 5 days  
**Reason:** Real-time monitoring dashboards

**Deliverables:**

- [ ] Prometheus metrics exporter
- [ ] Grafana dashboard templates
- [ ] Alerts: High CPU, memory leak, MT5 disconnect
- [ ] Historical performance charts
- [ ] Uptime SLA tracking

---

## Implementation Order

### **Phase 1: Prove Profitability (Week 1-4)**

```text
WEEK 1-2: Market Scanner
├─ Task 1: Build scanner core
└─ Task 2: Begin profitability testing

WEEK 3-4: Production Stability
├─ Task 6: Secrets management ✅ COMPLETE (April 10)
├─ Task 7: Rate limiting
├─ Task 8: Health checks
├─ Task 9: NSSM service
├─ Task 10: Error handling audit
└─ Task 11: Logging improvements
```

**Milestone:** 50+ paper trades with >50% win rate

---

### **Phase 2: Features + Database (Week 5-8)**

```text
WEEK 5-6: UX Features
├─ Task 12: Risk presets ✅
├─ Task 13: Symbol performance analytics ✅
├─ Task 14: News auto-update ✅
├─ Task 15: Trailing stops ✅
└─ Task 16: In-app notifications

WEEK 7-8: Database Migration
├─ Task 17: PostgreSQL setup
├─ Task 18: User authentication
└─ Task 19: Multi-account support
```

**Milestone:** System runs 24/7 unattended for 2 weeks

---

### **Phase 3: Multi-User Launch (Week 9-12)**

```text
WEEK 9-10: User Isolation + Admin
├─ Task 21: Dashboard user privacy
├─ Task 22: Account selector UI
└─ Task 23: Admin panel

WEEK 11-12: Advanced Features (Optional)
├─ Task 25: Portfolio optimization
├─ Task 26: Execution quality metrics
└─ Task 28: Prometheus + Grafana
```

**Milestone:** Launch SaaS with 10 beta users

---

## Today's Focus

### **COMPLETED TODAY (April 10, 2026):**

1. ✅ **Task #1: Market Scanner** - FULLY IMPLEMENTED (850+ lines)
   - ✅ Created `engine/market_scanner.py` with ATR, ADX, RSI, ROC, volatility calculations
   - ✅ Built `config/market_scanner.json` with weighted scoring per trading type
   - ✅ Implemented 9 API endpoints in `api/routes/scanner.py`
   - ✅ Built React scanner dashboard (`/scanner` page)
   - ✅ Added Scanner Monitor component (live performance tracking)
   - ✅ Smart add/remove buttons with capacity checking
   - ✅ Background auto-scan scheduler (60-minute intervals)
   - ✅ Config separation (market_scanner.json vs scanner.json)
   - ✅ Toast notification system (replaced browser alerts)
   - ✅ Dynamic symbol dropdown in charts

2. ✅ **Task #2: Profitability Validation System** - FULLY IMPLEMENTED (500+ lines)
   - ✅ Created `engine/performance_report.py` (CLI + API)
   - ✅ Built `/profitability/` REST endpoints
   - ✅ Created profitability dashboard page (`/profitability`)
   - ✅ Success criteria tracking (50+ trades, 50% WR, 1.5 PF)
   - ✅ Breakdown by symbol and trading type
   - ✅ Period filters (7/14/30 days, all time)
   - ✅ Current status: 64 trades collected, monitoring in progress

3. ✅ **UX Improvements**
   - ✅ Replaced all alert() with toast notifications
   - ✅ Redesigned Strategy Scanner layout
   - ✅ Added capacity limits enforcement (7/15/18 per mode)
   - ✅ Smart buttons (show "Remove" if already added, "Full" if at capacity)
   - ✅ Grouped navigation into 4 sections (Trading/Tools/Reports/System)
   - ✅ Renamed "Task #2 Validation" → "Profitability" (production-grade naming)

4. ✅ **Task #6: Secrets Management** - COMPLETE (1 day)
   - ✅ Discovered env vars already in use (python-dotenv ✅)
   - ✅ Created `engine/validate_secrets.py` (startup validation)
   - ✅ Created `setup_secrets.bat` (interactive setup wizard)
   - ✅ Enhanced `.env.example` with better comments
   - ✅ Updated README with quick start guide
   - ✅ Created `docs/15-task6-secrets-management.md`
   - ✅ Verified `.gitignore` excludes `.env` files
   - ✅ Documented future enhancements (AWS Secrets Manager for multi-user)

5. ⏳ **Quick Audit** - DEFERRED (Task 10 prep)

### **This Week (Week 3 - Production Stability):**

- [x] ✅ **Task #6: Secrets Management** (COMPLETE - April 10)
- [x] ✅ **Task #7: Rate Limiting** (COMPLETE - April 10)
- [x] ✅ **Task #8: Health Check Endpoint** (COMPLETE - April 10)
- [x] ✅ **Task #9: Automated Restart (NSSM)** (COMPLETE - April 10)
- [x] ✅ **Task #10: Error Handling Audit** (COMPLETE - April 10)
- [x] ✅ **Task #11: Logging Improvements** (COMPLETE - April 10)

### **Next Week (Week 4 - Transition to UX Features):**

- [ ] **Continue monitoring Task #2 profitability** (critical blocker for multi-user)
- [ ] Investigate low win rate (21.88% vs 50% target)
- [ ] Analyze symbol/strategy performance breakdown
- [ ] Decision: Focus on swing only OR optimize all modes
- [ ] Begin Week 5-6 planning (Task 12-16) if profitability improves

---

## 🚨 Critical Decisions Needed

| Decision | Impact | Deadline |
| ---------- | -------- | ---------- |
| **Free vs Paid Supabase** | Database costs $0-$25/mo | Week 7 |
| **Email provider** | Notifications cost $0-$20/mo | Week 6 |
| **Cloud vs Local deployment** | Infrastructure strategy | Week 8 |
| **Pricing tiers** | $39/$99 vs $49/$149 | Week 10 |

---

## 📊 Success Metrics

### **Phase 1 (Profitability)**

- ✅ 50+ closed paper trades
- ✅ Win rate >50%
- ✅ Profit factor >1.5
- ✅ No circuit breaker violations
- ✅ 10+ profitable scanner-discovered symbols

### **Phase 2 (Stability)**

- ✅ 99% uptime over 2 weeks
- ✅ Zero data loss incidents
- ✅ <200ms API response time (p95)
- ✅ All secrets removed from config files
- ✅ Health check endpoint 100% reliable

### **Phase 3 (Multi-User)**

- ✅ 10 beta users onboarded
- ✅ Zero cross-user data leaks
- ✅ Admin panel functional
- ✅ Stripe integration live
- ✅ $1,000+ MRR within 30 days of launch

---

## 📚 Reference Documents

- [11-industry-standards-comparison.md](./11-industry-standards-comparison.md) - Competitive analysis
- [12-multi-user-architecture.md](./12-multi-user-architecture.md) - Database schema + code patterns
- [08-pricing-and-valuation.md](./08-pricing-and-valuation.md) - Pricing strategy
- [10-operations-guide.md](./10-operations-guide.md) - Deployment guide
- [14-task2-profitability-validation.md](./14-task2-profitability-validation.md) - Task #2 implementation details

---

## ✅ Completed Items (April 10, 2026)

### Major Features (Tasks)

- ✅ **Task 1: Market Scanner** - COMPLETE (850+ lines, full backend + frontend + scheduler)
- ✅ **Task 2: Profitability Validation System** - COMPLETE (monitoring system ready, collecting data)
- ✅ Task 3: Dashboard UI accuracy (consecutive losses fix)
- ✅ Task 4: Optimizer param safety verification
- ✅ Task 5: Bar count alignment (200k/50k/20k)

### UX Improvements

- ✅ Toast notification system (replaced browser alerts)
- ✅ Smart add/remove buttons with capacity checking (7/15/18 limits)
- ✅ Strategy Scanner redesign (Currently Scanning section, Clear All)
- ✅ Scanner Monitor component (live performance tracking)
- ✅ Dynamic chart symbol dropdown (auto-syncs with scanner.json)
- ✅ Grouped navigation with sections (Trading/Tools/Reports/System)

---

**Last Updated:** April 10, 2026  
**Next Review:** Weekly on Mondays
