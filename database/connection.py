"""
Database connection pool and session management.
"""

import os
from contextlib import contextmanager
from typing import Generator

from loguru import logger
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://aibot:changeme123@localhost:5433/aibot_mt5")

# Connection pool settings (optimized for trading bot with ~5-10 concurrent requests)
POOL_SIZE = 10          # Core connections
MAX_OVERFLOW = 20       # Additional connections during spikes
POOL_TIMEOUT = 30       # Seconds to wait for connection
POOL_RECYCLE = 3600     # Recycle connections after 1 hour (prevent stale connections)

# ---------------------------------------------------------------------------
# Engine and Base
# ---------------------------------------------------------------------------

engine = create_engine(
    DATABASE_URL,
    poolclass=QueuePool,
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    pool_timeout=POOL_TIMEOUT,
    pool_pre_ping=True,      # Verify connections before use (handles DB restarts)
    pool_recycle=POOL_RECYCLE,
    echo=False,              # Set to True for SQL query logging (debug only)
)

# Enable SQLite-compatible PRAGMA for foreign key checks (PostgreSQL doesn't need this, but safe to have)
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_conn, connection_record):
    """Set connection-level pragmas (PostgreSQL specific settings can go here)."""
    # Example: cursor = dbapi_conn.cursor()
    # cursor.execute("SET timezone='UTC'")
    # cursor.close()
    pass

# ORM Base class (all models inherit from this)
Base = declarative_base()

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def get_db() -> Generator:
    """
    Dependency for FastAPI routes — provides a database session.
    
    Usage:
        @router.get("/users")
        def get_users(db: Session = Depends(get_db)):
            return db.query(User).all()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def get_db_context():
    """
    Context manager for non-FastAPI code (e.g., background tasks).
    
    Usage:
        with get_db_context() as db:
            user = db.query(User).first()
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Initialize database — create all tables.
    Called on first startup or after migrations.
    """
    from database.models import Base as ModelsBase  # Import to register all models
    
    try:
        ModelsBase.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
    except Exception as exc:
        logger.error(f"Database initialization failed: {exc}")
        raise


def test_connection() -> bool:
    """Test database connectivity — returns True if successful."""
    try:
        with engine.connect() as conn:
            result = conn.execute("SELECT 1").scalar()
            logger.info(f"Database connection OK (result: {result})")
            return True
    except Exception as exc:
        logger.error(f"Database connection failed: {exc}")
        return False
