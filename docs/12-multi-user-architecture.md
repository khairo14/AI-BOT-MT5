# Multi-User Architecture - Shared ML Models

**Last Updated:** April 10, 2026  
**Status:** Planning / Not Implemented  
**Purpose:** Define multi-tenant architecture with shared LSTM/RL models and per-user settings

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Database Schema](#database-schema)
3. [File System Structure](#file-system-structure)
4. [User Settings & Customization](#user-settings--customization)
5. [Training Behavior](#training-behavior)
6. [Code Changes Required](#code-changes-required)
7. [Migration Path](#migration-path)
8. [API Changes](#api-changes)

---

## Architecture Overview

### Core Principle: Shared Intelligence, Personal Control

```text
┌─────────────────────────────────────────────────────────────┐
│                    PLATFORM LEVEL (SHARED)                   │
├─────────────────────────────────────────────────────────────┤
│ LSTM Models:         27 models (221 MB total)               │
│ ├─ Apple_day_trading_lstm.pt                                │
│ ├─ EURUSD_scalping_lstm.pt                                  │
│ └─ ... (trained on ALL users' market data)                  │
│                                                              │
│ RL Q-Tables:         3 tables (trading types)               │
│ ├─ rl_qtable_scalping.json                                  │
│ ├─ rl_qtable_day_trading.json                               │
│ └─ rl_qtable_swing.json                                     │
│   (learned from ALL users' trade outcomes)                  │
│                                                              │
│ Default Parameters:  Shared optimized params                │
│ └─ Base stop loss, take profit, confidence thresholds       │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                     USER LEVEL (PERSONAL)                    │
├─────────────────────────────────────────────────────────────┤
│ MT5 Account:         Broker credentials, account #          │
│ Risk Settings:       Max drawdown, position size limits     │
│ Strategy Selection:  Enabled symbols/trading types          │
│ Parameter Overrides: Custom stop loss/TP per symbol         │
│ Positions:           Active trades, history                 │
│ Circuit Breakers:    Personal daily loss limits             │
└─────────────────────────────────────────────────────────────┘
```

**Benefits:**

- ✅ New users get expert-level models immediately
- ✅ Collective learning: 100 users = 100x faster improvement
- ✅ Low GPU cost: Train once, everyone benefits
- ✅ Personal control: Each user customizes risk/strategies
- ✅ Privacy: User positions/balances separate

---

## Database Schema

### PostgreSQL Schema (Recommended)

```sql
-- ============================================================
-- USERS & AUTHENTICATION
-- ============================================================

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,  -- bcrypt
    full_name VARCHAR(255),
    subscription_tier VARCHAR(50) DEFAULT 'free',  -- free, basic, pro
    account_status VARCHAR(50) DEFAULT 'active',   -- active, suspended, cancelled
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    last_login TIMESTAMP,
    
    -- Billing
    stripe_customer_id VARCHAR(255),
    subscription_end_date TIMESTAMP,
    
    -- Preferences (JSON for flexibility)
    preferences JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_status ON users(account_status);

-- ============================================================
-- MT5 ACCOUNTS (Users can have multiple broker accounts)
-- ============================================================

CREATE TABLE mt5_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    
    -- MT5 Connection
    account_number BIGINT NOT NULL,
    broker VARCHAR(100) NOT NULL,           -- XM, FTMO, etc.
    server VARCHAR(100) NOT NULL,           -- XM-MT5, FTMO-Demo, etc.
    password_encrypted TEXT NOT NULL,       -- AES-256 encrypted
    
    -- Account Mode
    account_mode VARCHAR(20) DEFAULT 'paper',  -- paper, live
    
    -- Status
    is_active BOOLEAN DEFAULT TRUE,
    is_primary BOOLEAN DEFAULT FALSE,       -- User's default account
    last_sync TIMESTAMP,
    connection_status VARCHAR(50),          -- connected, disconnected, error
    
    -- Metadata
    account_alias VARCHAR(100),             -- User-friendly name
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, account_number, broker)
);

CREATE INDEX idx_mt5_user ON mt5_accounts(user_id);
CREATE INDEX idx_mt5_active ON mt5_accounts(is_active);

-- ============================================================
-- USER RISK SETTINGS
-- ============================================================

CREATE TABLE user_risk_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mt5_account_id UUID REFERENCES mt5_accounts(id) ON DELETE CASCADE,
    
    -- Global Risk Limits
    max_daily_loss_pct NUMERIC(5,2) DEFAULT 2.00,      -- 2% max daily loss
    max_total_drawdown_pct NUMERIC(5,2) DEFAULT 10.00, -- 10% max drawdown
    max_position_size_pct NUMERIC(5,2) DEFAULT 2.00,   -- 2% per position
    max_open_positions INT DEFAULT 3,
    max_correlated_positions INT DEFAULT 2,
    
    -- Trading Hours (JSON: [{"open":"09:30","close":"16:00","timezone":"America/New_York"}])
    allowed_trading_hours JSONB,
    
    -- Risk Multipliers (applied to default params)
    stop_loss_multiplier NUMERIC(3,2) DEFAULT 1.00,    -- 0.5x - 2.0x
    take_profit_multiplier NUMERIC(3,2) DEFAULT 1.00,
    
    -- Circuit Breakers
    circuit_breaker_enabled BOOLEAN DEFAULT TRUE,
    max_consecutive_losses INT DEFAULT 3,
    cooldown_minutes INT DEFAULT 60,
    
    -- News Filter
    news_filter_enabled BOOLEAN DEFAULT TRUE,
    news_blackout_minutes INT DEFAULT 15,
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, mt5_account_id)
);

CREATE INDEX idx_risk_user ON user_risk_settings(user_id);

-- ============================================================
-- STRATEGY SELECTION (Which symbols/trading types enabled)
-- ============================================================

CREATE TABLE user_strategy_selection (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mt5_account_id UUID REFERENCES mt5_accounts(id) ON DELETE CASCADE,
    
    symbol VARCHAR(50) NOT NULL,
    trading_type VARCHAR(50) NOT NULL,  -- scalping, day_trading, swing
    
    is_enabled BOOLEAN DEFAULT TRUE,
    
    -- Auto-trading vs manual signals
    execution_mode VARCHAR(20) DEFAULT 'manual',  -- auto, manual
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, mt5_account_id, symbol, trading_type)
);

CREATE INDEX idx_strategy_user ON user_strategy_selection(user_id);
CREATE INDEX idx_strategy_enabled ON user_strategy_selection(is_enabled);

-- ============================================================
-- PARAMETER OVERRIDES (Custom optimized params per user)
-- ============================================================

CREATE TABLE user_param_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mt5_account_id UUID REFERENCES mt5_accounts(id) ON DELETE CASCADE,
    
    symbol VARCHAR(50) NOT NULL,
    trading_type VARCHAR(50) NOT NULL,
    
    -- Override specific params (NULL = use platform default)
    stop_loss NUMERIC(6,4),
    take_profit NUMERIC(6,4),
    confidence_threshold NUMERIC(4,3),
    rsi_oversold NUMERIC(5,2),
    rsi_overbought NUMERIC(5,2),
    ema_fast INT,
    ema_slow INT,
    
    -- Metadata
    source VARCHAR(50),  -- 'manual', 'personal_optimizer', 'preset_conservative'
    optimized_at TIMESTAMP,
    performance_stats JSONB,  -- Win rate, profit factor from personal backtest
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, mt5_account_id, symbol, trading_type)
);

CREATE INDEX idx_param_user ON user_param_overrides(user_id);

-- ============================================================
-- POSITIONS & TRADE HISTORY
-- ============================================================

CREATE TABLE positions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mt5_account_id UUID NOT NULL REFERENCES mt5_accounts(id) ON DELETE CASCADE,
    
    -- MT5 Position Data
    mt5_ticket BIGINT NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    trading_type VARCHAR(50) NOT NULL,
    direction VARCHAR(10) NOT NULL,  -- BUY, SELL
    
    -- Entry
    entry_price NUMERIC(12,5) NOT NULL,
    entry_time TIMESTAMP NOT NULL,
    lot_size NUMERIC(10,2) NOT NULL,
    stop_loss NUMERIC(12,5),
    take_profit NUMERIC(12,5),
    
    -- Exit
    exit_price NUMERIC(12,5),
    exit_time TIMESTAMP,
    exit_reason VARCHAR(50),  -- tp_hit, sl_hit, manual_close, trailing_stop
    
    -- PnL
    profit_loss NUMERIC(12,2),
    profit_loss_pct NUMERIC(8,4),
    commission NUMERIC(10,2),
    swap NUMERIC(10,2),
    
    -- Signal Context (for analysis)
    signal_confidence NUMERIC(4,3),
    regime VARCHAR(50),
    entry_signals JSONB,  -- {lstm_prediction, rl_action, strategy_signals}
    
    -- Status
    status VARCHAR(20) DEFAULT 'open',  -- open, closed, cancelled
    
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_positions_user ON positions(user_id);
CREATE INDEX idx_positions_account ON positions(mt5_account_id);
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_positions_symbol ON positions(symbol, trading_type);
CREATE INDEX idx_positions_entry_time ON positions(entry_time);

-- ============================================================
-- CIRCUIT BREAKER STATE (Per user tracking)
-- ============================================================

CREATE TABLE user_circuit_breaker_state (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mt5_account_id UUID NOT NULL REFERENCES mt5_accounts(id) ON DELETE CASCADE,
    
    trading_type VARCHAR(50) NOT NULL,
    
    -- State
    consecutive_losses INT DEFAULT 0,
    is_blocked BOOLEAN DEFAULT FALSE,
    blocked_until TIMESTAMP,
    
    -- Daily Stats
    daily_pnl NUMERIC(12,2) DEFAULT 0,
    daily_trades INT DEFAULT 0,
    last_trade_time TIMESTAMP,
    
    -- Reset tracking
    last_reset TIMESTAMP DEFAULT NOW(),
    
    updated_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, mt5_account_id, trading_type)
);

CREATE INDEX idx_breaker_user ON user_circuit_breaker_state(user_id);

-- ============================================================
-- PLATFORM ML MODELS METADATA (Shared models tracking)
-- ============================================================

CREATE TABLE ml_models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    model_type VARCHAR(50) NOT NULL,  -- lstm, rl_qtable
    symbol VARCHAR(50),
    trading_type VARCHAR(50),
    
    -- Model File
    file_path TEXT NOT NULL,
    file_size_bytes BIGINT,
    
    -- Training Metadata
    trained_at TIMESTAMP NOT NULL,
    training_samples INT,
    training_bars INT,
    epochs INT,
    
    -- Performance Metrics
    val_accuracy NUMERIC(5,4),      -- LSTM validation accuracy
    val_profit_factor NUMERIC(6,2), -- RL backtest profit factor
    metrics JSONB,                   -- Full training metrics
    
    -- Lineage
    trained_by VARCHAR(50) DEFAULT 'system',  -- system, admin, user_id (if manual)
    trigger_reason VARCHAR(100),              -- auto_8_losses, manual_retrain, scheduled
    
    created_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(model_type, symbol, trading_type, trained_at)
);

CREATE INDEX idx_models_type ON ml_models(model_type);
CREATE INDEX idx_models_symbol ON ml_models(symbol, trading_type);
CREATE INDEX idx_models_trained ON ml_models(trained_at DESC);

-- ============================================================
-- SHARED TRADE MEMORY (All users contribute, all benefit)
-- ============================================================

CREATE TABLE shared_trade_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Attribution (anonymized for shared learning)
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,  -- NULL if user deleted
    
    symbol VARCHAR(50) NOT NULL,
    trading_type VARCHAR(50) NOT NULL,
    direction VARCHAR(10) NOT NULL,
    
    -- Trade Outcome
    profit_loss_pct NUMERIC(8,4) NOT NULL,
    hold_duration_minutes INT NOT NULL,
    
    -- Context (for RL learning)
    regime VARCHAR(50),
    confidence NUMERIC(4,3),
    entry_signals JSONB,
    exit_reason VARCHAR(50),
    
    traded_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_trade_memory_symbol ON shared_trade_memory(symbol, trading_type);
CREATE INDEX idx_trade_memory_date ON shared_trade_memory(traded_at DESC);

-- ============================================================
-- AUDIT LOG (Track all critical actions)
-- ============================================================

CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    mt5_account_id UUID REFERENCES mt5_accounts(id) ON DELETE SET NULL,
    
    action VARCHAR(100) NOT NULL,  -- login, trade_open, param_override, account_mode_switch
    entity_type VARCHAR(50),       -- position, mt5_account, risk_settings
    entity_id UUID,
    
    -- Context
    ip_address VARCHAR(45),
    user_agent TEXT,
    changes JSONB,  -- Before/after values
    
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_created ON audit_log(created_at DESC);
```

---

## File System Structure

### Shared ML Assets (Platform-Level)

```text
ai/models/                          # SHARED by all users
├─ Apple_day_trading_lstm.pt        # 8.2 MB
├─ Apple_day_trading_anchor.pt      # 8.2 MB
├─ Apple_day_trading_meta.json      # Training metadata
├─ EURUSD_scalping_lstm.pt
├─ EURUSD_scalping_anchor.pt
└─ ... (27 models total = ~221 MB)

ai/data/                            # SHARED RL & memory
├─ rl_qtable_scalping.json          # All users' scalping trades
├─ rl_qtable_day_trading.json       # All users' day trades
├─ rl_qtable_swing.json             # All users' swing trades
└─ trade_memory.jsonl               # Pooled trade history

config/                             # Platform defaults
├─ optimized_params.json            # Shared default params
├─ strategies.json                  # Available strategies
├─ symbols.json                     # Supported symbols
└─ app.json                         # Platform config
```

### User-Specific Data (Database Only)

```text
All user-specific data stored in PostgreSQL:
- MT5 credentials (encrypted)
- Risk settings
- Strategy selections
- Parameter overrides
- Positions & history
- Circuit breaker state

NO per-user files needed (except logs)
```

### Logs (Optional: Per-User)

```text
logs/
├─ system.log                       # Platform-level errors
└─ users/
   ├─ user_<uuid>_2026-04-10.log    # Daily user activity
   └─ user_<uuid>_2026-04-11.log
```

---

## User Settings & Customization

### Tier-Based Feature Access

```python
SUBSCRIPTION_TIERS = {
    "free": {
        "max_mt5_accounts": 1,
        "max_open_positions": 2,
        "strategies_enabled": ["EURUSD_scalping"],  # Limited symbols
        "can_override_params": False,
        "can_run_personal_optimizer": False,
        "execution_mode": "manual",  # Must approve signals
        "api_rate_limit": "10/min"
    },
    "basic": {  # $49/month
        "max_mt5_accounts": 2,
        "max_open_positions": 5,
        "strategies_enabled": "all",
        "can_override_params": True,   # Custom stop loss/TP
        "can_run_personal_optimizer": False,
        "execution_mode": "auto",      # Auto-execute signals
        "api_rate_limit": "60/min"
    },
    "pro": {  # $149/month
        "max_mt5_accounts": 5,
        "max_open_positions": 10,
        "strategies_enabled": "all",
        "can_override_params": True,
        "can_run_personal_optimizer": True,  # Run private optimizer
        "execution_mode": "auto",
        "api_rate_limit": "unlimited",
        "custom_webhooks": True,
        "export_data": True
    }
}
```

### Risk Preference Presets

Users can choose preset risk profiles:

```python
RISK_PRESETS = {
    "conservative": {
        "stop_loss_multiplier": 0.7,      # Tighter stops
        "take_profit_multiplier": 1.3,    # Wider targets
        "max_daily_loss_pct": 1.0,        # 1% max daily loss
        "max_position_size_pct": 1.0,
        "max_consecutive_losses": 2,
        "confidence_threshold_boost": 0.1  # Only take high-confidence
    },
    "moderate": {
        "stop_loss_multiplier": 1.0,      # Default params
        "take_profit_multiplier": 1.0,
        "max_daily_loss_pct": 2.0,
        "max_position_size_pct": 2.0,
        "max_consecutive_losses": 3,
        "confidence_threshold_boost": 0.0
    },
    "aggressive": {
        "stop_loss_multiplier": 1.5,      # Wider stops
        "take_profit_multiplier": 0.8,    # Tighter targets
        "max_daily_loss_pct": 5.0,
        "max_position_size_pct": 3.0,
        "max_consecutive_losses": 5,
        "confidence_threshold_boost": -0.1  # More signals
    }
}
```

### Parameter Override Examples

```python
# Example 1: User wants tighter stop loss on EURUSD scalping
user_param_overrides = {
    "user_id": "550e8400-e29b-41d4-a716-446655440000",
    "symbol": "EURUSD",
    "trading_type": "scalping",
    "stop_loss": 0.0010,        # Custom: 10 pips (vs default 15 pips)
    "take_profit": None,         # Use platform default
    "confidence_threshold": 0.75 # Higher threshold (vs default 0.65)
}

# Example 2: Conservative user disables scalping (too risky)
user_strategy_selection = [
    {"symbol": "EURUSD", "trading_type": "day_trading", "is_enabled": True},
    {"symbol": "EURUSD", "trading_type": "swing", "is_enabled": True},
    {"symbol": "EURUSD", "trading_type": "scalping", "is_enabled": False},  # Disabled
]
```

---

## Training Behavior

### LSTM Training (Shared)

```python
# Current: Single user
def check_auto_retrain(symbol, trading_type):
    recent = get_recent_trades(symbol, trading_type, limit=8)
    if all(t.profit_loss < 0 for t in recent):
        train_lstm(symbol, trading_type)

# Multi-User: Platform-level trigger
def check_auto_retrain(symbol, trading_type):
    # Aggregate all users' trades
    recent_platform = db.query("""
        SELECT profit_loss FROM positions
        WHERE symbol = %s AND trading_type = %s
        AND status = 'closed'
        ORDER BY exit_time DESC LIMIT 8
    """, (symbol, trading_type))
    
    if all(t.profit_loss < 0 for t in recent_platform):
        # Train shared model
        await train_lstm_async(symbol, trading_type)
        # All users benefit from updated model
```

**Key Changes:**

- Trigger based on **platform-wide** performance, not per-user
- One user's bad streak doesn't trigger retrain (need 8 collective losses)
- Training uses market OHLCV data (same 200k bars for everyone)
- Updated model replaces shared file `Apple_day_trading_lstm.pt`

### RL Q-Table Learning (Shared)

```python
# Current: Single user Q-table
def update_rl_qtable(trading_type, state, action, reward):
    qtable = load(f"ai/data/rl_qtable_{trading_type}_paper.json")
    qtable[state][action] = update_q_value(...)
    save(qtable)

# Multi-User: Shared Q-table
def update_rl_qtable(user_id, trading_type, state, action, reward):
    # Load shared Q-table
    qtable = load(f"ai/data/rl_qtable_{trading_type}.json")
    
    # Update with this user's trade outcome
    qtable[state][action] = update_q_value(...)
    
    # Save shared Q-table (all users benefit)
    save(qtable)
    
    # Log to shared memory
    db.insert_shared_trade_memory(
        user_id=user_id,
        trading_type=trading_type,
        state=state,
        action=action,
        reward=reward
    )
```

**Key Changes:**

- Every user's trade updates the shared Q-table
- 100 users = 100x more learning samples
- Faster convergence to optimal strategy
- New users get pre-trained Q-table (not empty)

### Parameter Optimizer (Hybrid)

```python
# Platform: Runs weekly on shared data
@scheduler.scheduled_job('cron', day_of_week='sun', hour=2)
async def platform_optimizer():
    for symbol in SYMBOLS:
        for trading_type in TRADING_TYPES:
            # Optimize on ALL users' trade history
            params = await optimize_params_shared(symbol, trading_type)
            
            # Save as platform defaults
            save_default_params(symbol, trading_type, params)
            
            # Notify users of new defaults
            await notify_param_update(symbol, trading_type, params)

# User: Can run personal optimizer (Pro tier only)
async def user_personal_optimizer(user_id, symbol, trading_type):
    # Check subscription tier
    if user.subscription_tier != "pro":
        raise PermissionDenied("Personal optimizer requires Pro tier")
    
    # Optimize on THIS user's trade history only
    user_trades = get_user_trades(user_id, symbol, trading_type)
    
    if len(user_trades) < 30:
        raise ValueError("Need at least 30 trades for personal optimization")
    
    params = await optimize_params(user_trades)
    
    # Save as user override
    db.save_user_param_override(
        user_id=user_id,
        symbol=symbol,
        trading_type=trading_type,
        params=params,
        source="personal_optimizer"
    )
```

**Key Changes:**

- **Platform optimizer**: Weekly run on pooled data → Updates defaults
- **Personal optimizer**: Pro users can optimize on their own trades
- Users choose: "Use platform defaults" or "Use my custom params"

---

## Code Changes Required

### 1. User Context Injection

**File:** `api/dependencies.py`

```python
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

security = HTTPBearer()

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
):
    """Extract user from JWT token."""
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.JWT_SECRET,
            algorithms=["HS256"]
        )
        user_id = payload.get("sub")
        
        user = await db.get(User, user_id)
        if not user or user.account_status != "active":
            raise HTTPException(status_code=401, detail="Invalid user")
        
        return user
    except jwt.JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

async def get_user_mt5_account(
    user: User = Depends(get_current_user),
    account_id: str = None,  # Optional: specific account, else primary
    db: AsyncSession = Depends(get_db)
):
    """Get user's MT5 account."""
    if account_id:
        account = await db.query(MT5Account).filter(
            MT5Account.id == account_id,
            MT5Account.user_id == user.id,
            MT5Account.is_active == True
        ).first()
    else:
        # Get primary account
        account = await db.query(MT5Account).filter(
            MT5Account.user_id == user.id,
            MT5Account.is_primary == True,
            MT5Account.is_active == True
        ).first()
    
    if not account:
        raise HTTPException(status_code=404, detail="MT5 account not found")
    
    return account
```

### 2. Signal Generation (User-Aware)

**File:** `api/signal_bus.py`

```python
# Current: Global signal generation
async def generate_signals():
    for symbol in SYMBOLS:
        signal = await check_strategy(symbol, trading_type)
        if signal:
            await execute_trade(signal)

# Multi-User: Per-user signal generation
async def generate_signals_for_user(user_id: str, mt5_account_id: str):
    # Get user's enabled strategies
    strategies = await db.get_user_strategies(user_id, mt5_account_id)
    
    # Get user's risk settings
    risk = await db.get_user_risk_settings(user_id, mt5_account_id)
    
    for strategy in strategies:
        if not strategy.is_enabled:
            continue
        
        # Check if circuit breaker allows trading
        if await is_circuit_breaker_blocked(user_id, mt5_account_id, strategy.trading_type):
            continue
        
        # Generate signal using SHARED LSTM model
        lstm_pred = predict_lstm(strategy.symbol, strategy.trading_type)  # Shared model
        
        # Get user's custom params or platform defaults
        params = await get_effective_params(user_id, strategy.symbol, strategy.trading_type)
        
        # Generate signal
        signal = await check_strategy(
            symbol=strategy.symbol,
            trading_type=strategy.trading_type,
            params=params,
            lstm_confidence=lstm_pred.confidence
        )
        
        if signal:
            # Apply user risk limits
            position_size = calculate_position_size(
                signal=signal,
                risk_settings=risk,
                account_balance=await get_account_balance(mt5_account_id)
            )
            
            if strategy.execution_mode == "auto":
                await execute_trade(user_id, mt5_account_id, signal, position_size)
            else:
                await notify_signal(user_id, signal)  # Manual approval needed

async def get_effective_params(user_id: str, symbol: str, trading_type: str):
    """Get user overrides if exist, else platform defaults."""
    override = await db.get_user_param_override(user_id, symbol, trading_type)
    
    if override:
        # Merge: user overrides take precedence
        defaults = load_default_params(symbol, trading_type)
        return {**defaults, **override}
    else:
        # Use platform defaults
        return load_default_params(symbol, trading_type)
```

### 3. Position Management (User-Scoped)

**File:** `engine/order_manager.py`

```python
# Current: Global positions
class OrderManager:
    def __init__(self):
        self.positions = []

# Multi-User: Scoped to user + account
class OrderManager:
    async def get_user_positions(self, user_id: str, mt5_account_id: str):
        return await db.query(Position).filter(
            Position.user_id == user_id,
            Position.mt5_account_id == mt5_account_id,
            Position.status == "open"
        ).all()
    
    async def open_position(
        self,
        user_id: str,
        mt5_account_id: str,
        symbol: str,
        trading_type: str,
        direction: str,
        lot_size: float,
        stop_loss: float,
        take_profit: float
    ):
        # Get MT5 account credentials
        account = await db.get(MT5Account, mt5_account_id)
        
        # Connect to user's MT5
        mt5 = MT5Client(
            login=account.account_number,
            password=decrypt(account.password_encrypted),
            server=account.server
        )
        
        # Execute trade
        ticket = mt5.open_trade(symbol, direction, lot_size, stop_loss, take_profit)
        
        # Save to database
        position = Position(
            user_id=user_id,
            mt5_account_id=mt5_account_id,
            mt5_ticket=ticket,
            symbol=symbol,
            trading_type=trading_type,
            direction=direction,
            entry_price=mt5.get_current_price(symbol),
            entry_time=datetime.now(),
            lot_size=lot_size,
            stop_loss=stop_loss,
            take_profit=take_profit,
            status="open"
        )
        await db.add(position)
        await db.commit()
        
        return position
```

### 4. LSTM Predictor (Unchanged - Shared Model)

**File:** `ai/predictor.py`

```python
# NO CHANGES NEEDED - Already uses shared models
def predict_lstm(symbol: str, trading_type: str):
    """Predict using shared LSTM model."""
    model_path = f"ai/models/{symbol}_{trading_type}_lstm.pt"
    
    # Load shared model (same for all users)
    model = load_model(model_path)
    
    # Predict
    prediction = model.forward(features)
    
    return {
        "direction": "BUY" if prediction > 0.5 else "SELL",
        "confidence": abs(prediction - 0.5) * 2
    }
```

**Key Point:** LSTM code stays SAME. All users use same model file.

### 5. RL Agent (Shared Q-Table)

**File:** `ai/rl_agent.py`

```python
# Current: Load user-specific Q-table
class RLAgent:
    def __init__(self, trading_type: str, account_mode: str):
        self.qtable = load(f"ai/data/rl_qtable_{trading_type}_{account_mode}.json")

# Multi-User: Load shared Q-table
class RLAgent:
    def __init__(self, trading_type: str):
        # Load shared Q-table (no account_mode split)
        self.qtable = load(f"ai/data/rl_qtable_{trading_type}.json")
    
    def update(self, user_id: str, state: str, action: str, reward: float):
        """Update shared Q-table with user's trade outcome."""
        # Standard Q-learning update
        old_q = self.qtable[state][action]
        max_future_q = max(self.qtable[next_state].values())
        new_q = old_q + ALPHA * (reward + GAMMA * max_future_q - old_q)
        
        self.qtable[state][action] = new_q
        
        # Save shared Q-table
        save(f"ai/data/rl_qtable_{self.trading_type}.json", self.qtable)
        
        # Log to shared memory (for analytics)
        db.insert_shared_trade_memory(
            user_id=user_id,
            trading_type=self.trading_type,
            state=state,
            action=action,
            reward=reward
        )
```

---

## Migration Path

### Phase 1: Database Setup (Week 1-2)

1. **Install PostgreSQL**

   ```bash
   # Local dev: Use Docker
   docker run -d \
     --name ai-bot-postgres \
     -e POSTGRES_PASSWORD=devpassword \
     -e POSTGRES_DB=aibot \
     -p 5432:5432 \
     postgres:15
   
   # Production: Supabase (free tier)
   # Or: AWS RDS, DigitalOcean, etc.
   ```

2. **Run Schema Migrations**

   ```bash
   # Using Alembic
   alembic revision --autogenerate -m "Initial schema"
   alembic upgrade head
   ```

3. **Seed Platform Defaults**

   ```python
   # Migrate config/*.json to database
   python scripts/migrate_configs_to_db.py
   ```

### Phase 2: Single-User Compatibility (Week 2-3)

```python
# Create "admin" user representing current single-user system
admin_user = User(
    id="00000000-0000-0000-0000-000000000001",
    email="admin@localhost",
    subscription_tier="pro",
    account_status="active"
)

# Migrate existing MT5 account
mt5_account = MT5Account(
    user_id=admin_user.id,
    account_number=config["mt5"]["login"],
    broker="XM",
    server="XMGlobal-MT5",
    password_encrypted=encrypt(config["mt5"]["password"]),
    is_primary=True
)

# Migrate existing positions
for pos in old_positions:
    new_pos = Position(
        user_id=admin_user.id,
        mt5_account_id=mt5_account.id,
        **pos
    )
```

**System works identically for single "admin" user.**

### Phase 3: Multi-User Rollout (Week 4+)

1. **Add Authentication**
   - JWT login/register endpoints
   - Password hashing (bcrypt)
   - Email verification

2. **User Onboarding Flow**

    ```text
   Register → Email Verify → Add MT5 Account → Choose Risk Preset → Enable Strategies → Start Trading
   ```

3. **Dashboard Updates**
   - User login page
   - Account selector (for multi-account users)
   - Settings pages (risk, strategies, params)

4. **Subscription & Billing**
   - Stripe integration
   - Free tier with 1 symbol
   - Upgrade flow to Basic/Pro

---

## API Changes

### Authentication Endpoints

```python
# POST /auth/register
{
  "email": "user@example.com",
  "password": "SecurePass123!",
  "full_name": "John Doe"
}
→ Returns: { "user_id": "uuid", "email": "...", "verification_sent": true }

# POST /auth/login
{
  "email": "user@example.com",
  "password": "SecurePass123!"
}
→ Returns: { "access_token": "jwt...", "user": {...} }

# POST /auth/logout
Authorization: Bearer <token>
→ Returns: { "message": "Logged out" }
```

### MT5 Account Management

```python
# GET /users/me/mt5-accounts
Authorization: Bearer <token>
→ Returns: [
    {
      "id": "uuid",
      "account_number": 12345678,
      "broker": "XM",
      "is_primary": true,
      "connection_status": "connected"
    }
  ]

# POST /users/me/mt5-accounts
Authorization: Bearer <token>
{
  "account_number": 87654321,
  "broker": "XM",
  "server": "XMGlobal-MT5",
  "password": "mt5password"
}
→ Returns: { "id": "uuid", "status": "pending_verification" }
```

### User Settings

```python
# GET /users/me/risk-settings
Authorization: Bearer <token>
→ Returns: {
    "max_daily_loss_pct": 2.0,
    "max_position_size_pct": 2.0,
    "circuit_breaker_enabled": true,
    ...
  }

# PUT /users/me/risk-settings
Authorization: Bearer <token>
{
  "max_daily_loss_pct": 1.5,
  "stop_loss_multiplier": 0.8
}
→ Returns: { "updated": true, "settings": {...} }

# GET /users/me/strategies
Authorization: Bearer <token>
→ Returns: [
    {
      "symbol": "EURUSD",
      "trading_type": "scalping",
      "is_enabled": true,
      "execution_mode": "auto"
    },
    ...
  ]

# PUT /users/me/strategies/{symbol}/{trading_type}
Authorization: Bearer <token>
{
  "is_enabled": false
}
→ Returns: { "updated": true }
```

### Positions (User-Scoped)

```python
# GET /positions
Authorization: Bearer <token>
Optional Query: ?mt5_account_id=uuid, ?status=open
→ Returns: [
    {
      "id": "uuid",
      "symbol": "EURUSD",
      "direction": "BUY",
      "entry_price": 1.0850,
      "profit_loss": 45.20,
      "status": "open"
    }
  ]

# POST /positions
Authorization: Bearer <token>
{
  "symbol": "EURUSD",
  "trading_type": "scalping",
  "direction": "BUY",
  "lot_size": 0.1
}
→ Returns: { "position_id": "uuid", "ticket": 123456, "status": "opened" }
```

### Platform ML Models (Read-Only for Users)

```python
# GET /ml/models
→ Returns: [
    {
      "symbol": "EURUSD",
      "trading_type": "scalping",
      "trained_at": "2026-04-10T02:15:00Z",
      "val_accuracy": 0.6847,
      "training_samples": 200000
    }
  ]

# GET /ml/models/{symbol}/{trading_type}/performance
→ Returns: {
    "symbol": "EURUSD",
    "trading_type": "scalping",
    "last_30_days": {
      "total_signals": 127,
      "win_rate": 0.583,
      "avg_profit_per_signal": 12.4
    }
  }
```

---

## Summary

### What's Shared (Platform-Level)

✅ **LSTM Models** (27 models, 221 MB)  
✅ **RL Q-Tables** (3 trading types)  
✅ **Default Optimized Parameters**  
✅ **Trade Memory** (pooled learning)  
✅ **Training Logic** (train once, all benefit)

### What's Per-User (Individual)

✅ **MT5 Accounts** (credentials, broker)  
✅ **Risk Settings** (max loss, position size)  
✅ **Strategy Selection** (enabled symbols/types)  
✅ **Parameter Overrides** (custom stops/TPs)  
✅ **Positions & History** (trades, PnL)  
✅ **Circuit Breaker State** (consecutive losses)  
✅ **Subscription Tier** (free/basic/pro)

### Benefits

- 🚀 **Faster Learning**: 100 users = 100x more trade data for RL
- 💰 **Lower Costs**: Train once vs per-user ($6/mo vs $600/mo GPU)
- 🎯 **Better Models**: Collective intelligence > individual
- ⚡ **Instant Onboarding**: New users get expert models immediately
- 🔒 **Privacy**: User positions/balances isolated
- 🎛️ **Personalization**: Each user controls risk/strategies/params

---

**Next Steps:**

1. Review this architecture
2. Decide on deployment (Cloudflare Tunnel vs cloud DB)
3. Start Week 1 implementation (database setup, migration scripts)
4. Build authentication & user management
5. Update dashboard for multi-user
