"""
SQLAlchemy ORM models for AI-BOT-MT5 multi-user architecture.

Tables:
1. users — User accounts (email, password, permissions)
2. mt5_accounts — MT5 broker credentials (encrypted passwords)
3. user_risk_settings — Per-user risk limits
4. user_strategy_selection — Enabled strategies per user
5. user_param_overrides — Custom strategy parameters
6. positions — Open/closed trade positions
7. user_circuit_breaker_state — Per-user drawdown state
8. ml_models — ML model metadata (not .pt files)
9. shared_trade_memory — Trade history for RL agent
10. audit_log — User actions for compliance
"""

import enum
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database.connection import Base


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    """User permission levels."""
    ADMIN = "admin"      # Full access, can manage other users
    USER = "user"        # Normal user, owns their own data
    READONLY = "readonly"  # View-only access (for observers)


class AccountMode(str, enum.Enum):
    """Trading account mode."""
    PAPER = "paper"  # Demo/paper trading
    LIVE = "live"    # Real money trading


class TradingMode(str, enum.Enum):
    """Trading strategy mode."""
    SCALPING = "scalping"
    DAY_TRADING = "day_trading"
    SWING = "swing"


class PositionStatus(str, enum.Enum):
    """Position lifecycle status."""
    OPEN = "open"
    CLOSED = "closed"


class SignalStatus(str, enum.Enum):
    """Signal processing status."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class User(Base):
    """User account table — authentication and permissions."""
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.USER)
    is_active = Column(Boolean, nullable=False, default=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))
    last_login_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    mt5_accounts = relationship("MT5Account", back_populates="user", cascade="all, delete-orphan")
    risk_settings = relationship("UserRiskSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    strategy_selections = relationship("UserStrategySelection", back_populates="user", cascade="all, delete-orphan")
    param_overrides = relationship("UserParamOverride", back_populates="user", cascade="all, delete-orphan")
    positions = relationship("Position", back_populates="user", cascade="all, delete-orphan")
    circuit_breaker_state = relationship("UserCircuitBreakerState", back_populates="user", uselist=False, cascade="all, delete-orphan")
    audit_logs = relationship("AuditLog", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(id={self.id}, email={self.email}, role={self.role})>"


class MT5Account(Base):
    """MT5 broker account credentials — stores encrypted passwords."""
    __tablename__ = "mt5_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    account_mode = Column(Enum(AccountMode), nullable=False)
    login = Column(String(50), nullable=False)
    encrypted_password = Column(Text, nullable=False)  # AES-256 encrypted
    server = Column(String(100), nullable=False)
    is_primary = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    last_connected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="mt5_accounts")

    __table_args__ = (
        Index("idx_mt5_user_mode", "user_id", "account_mode"),
    )

    def __repr__(self):
        return f"<MT5Account(id={self.id}, login={self.login}, mode={self.account_mode})>"


class UserRiskSettings(Base):
    """Per-user risk management settings."""
    __tablename__ = "user_risk_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    
    # Risk limits
    max_daily_drawdown_pct = Column(Float, nullable=False, default=3.0)
    max_weekly_drawdown_pct = Column(Float, nullable=False, default=7.0)
    max_risk_per_trade_pct = Column(Float, nullable=False, default=1.0)
    max_open_positions = Column(Integer, nullable=False, default=5)
    
    # Position sizing
    default_lot_size = Column(Float, nullable=False, default=0.01)
    use_dynamic_sizing = Column(Boolean, nullable=False, default=True)
    
    # Trading hours (JSON: {trading_mode: {enabled: bool, start_hour: int, end_hour: int}})
    trading_hours = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="risk_settings")

    def __repr__(self):
        return f"<UserRiskSettings(user_id={self.user_id}, max_positions={self.max_open_positions})>"


class UserStrategySelection(Base):
    """Per-user enabled strategies and symbols."""
    __tablename__ = "user_strategy_selections"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    trading_mode = Column(Enum(TradingMode), nullable=False)
    symbol = Column(String(20), nullable=False)
    strategy_name = Column(String(50), nullable=False)
    is_enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="strategy_selections")

    __table_args__ = (
        Index("idx_strategy_user_mode", "user_id", "trading_mode"),
        Index("idx_strategy_user_symbol", "user_id", "symbol"),
    )

    def __repr__(self):
        return f"<UserStrategySelection(user_id={self.user_id}, strategy={self.strategy_name}, symbol={self.symbol})>"


class UserParamOverride(Base):
    """Custom strategy parameters per user."""
    __tablename__ = "user_param_overrides"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    strategy_name = Column(String(50), nullable=False)
    symbol = Column(String(20), nullable=False)
    params = Column(JSON, nullable=False)  # {param_name: value}
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="param_overrides")

    __table_args__ = (
        Index("idx_param_user_strategy", "user_id", "strategy_name", "symbol"),
    )

    def __repr__(self):
        return f"<UserParamOverride(user_id={self.user_id}, strategy={self.strategy_name}, symbol={self.symbol})>"


class Position(Base):
    """Trade positions — replaces JSON files for multi-user."""
    __tablename__ = "positions"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticket = Column(Integer, nullable=False, unique=True, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    direction = Column(String(10), nullable=False)  # BUY or SELL
    trading_mode = Column(Enum(TradingMode), nullable=False)
    strategy_name = Column(String(50), nullable=False)
    
    # Entry
    entry_price = Column(Float, nullable=False)
    entry_time = Column(DateTime(timezone=True), nullable=False)
    lot_size = Column(Float, nullable=False)
    
    # Exit levels
    sl = Column(Float, nullable=True)
    tp = Column(Float, nullable=True)
    
    # Exit (null if still open)
    exit_price = Column(Float, nullable=True)
    exit_time = Column(DateTime(timezone=True), nullable=True)
    profit = Column(Float, nullable=True)
    
    # Metadata
    status = Column(Enum(PositionStatus), nullable=False, default=PositionStatus.OPEN, index=True)
    comment = Column(Text, nullable=True)
    magic_number = Column(Integer, nullable=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="positions")

    __table_args__ = (
        Index("idx_position_user_status", "user_id", "status"),
        Index("idx_position_user_symbol", "user_id", "symbol"),
        Index("idx_position_entry_time", "entry_time"),
    )

    def __repr__(self):
        return f"<Position(ticket={self.ticket}, symbol={self.symbol}, status={self.status})>"


class UserCircuitBreakerState(Base):
    """Per-user circuit breaker drawdown state."""
    __tablename__ = "user_circuit_breaker_state"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    
    # Balances
    day_start_balance = Column(Float, nullable=False, default=0.0)
    week_start_balance = Column(Float, nullable=False, default=0.0)
    current_balance = Column(Float, nullable=False, default=0.0)
    
    # Drawdown
    daily_drawdown_pct = Column(Float, nullable=False, default=0.0)
    weekly_drawdown_pct = Column(Float, nullable=False, default=0.0)
    
    # Circuit breaker status
    is_paused = Column(Boolean, nullable=False, default=False)
    pause_reason = Column(String(255), nullable=True)
    paused_at = Column(DateTime(timezone=True), nullable=True)
    
    # Reset timestamps
    last_day_reset = Column(DateTime(timezone=True), nullable=True)
    last_week_reset = Column(DateTime(timezone=True), nullable=True)
    
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    # Relationships
    user = relationship("User", back_populates="circuit_breaker_state")

    def __repr__(self):
        return f"<UserCircuitBreakerState(user_id={self.user_id}, paused={self.is_paused})>"


class MLModel(Base):
    """ML model metadata — tracks trained models per symbol/mode."""
    __tablename__ = "ml_models"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    symbol = Column(String(20), nullable=False, index=True)
    trading_mode = Column(Enum(TradingMode), nullable=False)
    model_type = Column(String(50), nullable=False)  # "lstm" or "anchor"
    
    # File paths (relative to ai/models/)
    model_path = Column(String(255), nullable=False)
    meta_path = Column(String(255), nullable=True)
    
    # Training metadata
    trained_at = Column(DateTime(timezone=True), nullable=False)
    training_samples = Column(Integer, nullable=True)
    validation_score = Column(Float, nullable=True)
    hyperparams = Column(JSON, nullable=True)
    
    # Status
    is_active = Column(Boolean, nullable=False, default=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), onupdate=datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_model_symbol_mode", "symbol", "trading_mode"),
    )

    def __repr__(self):
        return f"<MLModel(symbol={self.symbol}, mode={self.trading_mode}, type={self.model_type})>"


class SharedTradeMemory(Base):
    """Shared trade history for RL agent — stores outcomes for learning."""
    __tablename__ = "shared_trade_memory"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    trading_mode = Column(Enum(TradingMode), nullable=False, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    strategy_name = Column(String(50), nullable=False)
    
    # Trade outcome
    direction = Column(String(10), nullable=False)  # BUY or SELL
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    profit = Column(Float, nullable=False)
    
    # RL features (for Q-table learning)
    confidence = Column(Float, nullable=False)
    regime = Column(String(20), nullable=True)
    rr_ratio = Column(Float, nullable=True)
    
    # Timestamps
    entry_time = Column(DateTime(timezone=True), nullable=False)
    exit_time = Column(DateTime(timezone=True), nullable=False)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"))

    __table_args__ = (
        Index("idx_memory_mode_symbol", "trading_mode", "symbol"),
        Index("idx_memory_entry_time", "entry_time"),
    )

    def __repr__(self):
        return f"<SharedTradeMemory(symbol={self.symbol}, mode={self.trading_mode}, profit={self.profit})>"


class AuditLog(Base):
    """User action audit log — tracks all system interactions."""
    __tablename__ = "audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, server_default=text("uuid_generate_v4()"))
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)  # "login", "trade_executed", "risk_settings_updated"
    details = Column(JSON, nullable=True)  # {key: value} metadata
    ip_address = Column(String(45), nullable=True)  # IPv4 or IPv6
    user_agent = Column(Text, nullable=True)
    
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=text("CURRENT_TIMESTAMP"), index=True)

    # Relationships
    user = relationship("User", back_populates="audit_logs")

    __table_args__ = (
        Index("idx_audit_user_action", "user_id", "action"),
        Index("idx_audit_created", "created_at"),
    )

    def __repr__(self):
        return f"<AuditLog(action={self.action}, user_id={self.user_id}, created_at={self.created_at})>"
