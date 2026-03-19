"""
FastAPI application entry point — Phase 3.
Mounts all REST routes and the WebSocket live feed.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.routes import account, trades, signals, config, ai as ai_routes, risk as risk_routes, backtest as backtest_routes
from api.websocket.feed import router as ws_router
from api.signal_bus import bus, _set_event_loop
from api.runner_loop import start_runner_loop
from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager

# ---------------------------------------------------------------------------
# Logging setup — write to file so dashboard can tail it
# ---------------------------------------------------------------------------
_LOG_FILE = Path("logs/api.log")
_LOG_FILE.parent.mkdir(exist_ok=True)
logger.remove()  # drop default stderr sink
logger.add(sys.stderr, level="INFO", colorize=True,
           format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> | <level>{message}</level>")
logger.add(str(_LOG_FILE), level="DEBUG", rotation="10 MB", retention=3, encoding="utf-8",
           format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}")

# ---------------------------------------------------------------------------
# Shared MT5 client — created once at startup, closed at shutdown
# ---------------------------------------------------------------------------
mt5_client: MT5Client | None = None
_risk_manager: RiskManager | None = None


def get_mt5_client() -> "MT5Client | None":
    """Return the application-level MT5 client instance."""
    return mt5_client


def get_risk_manager() -> "RiskManager | None":
    """Return the shared RiskManager instance (created at startup)."""
    return _risk_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mt5_client, _risk_manager
    logger.info("Starting EVOTRADE-AI API...")
    # Register the running event loop so thread executors can schedule coroutines safely
    _set_event_loop(asyncio.get_event_loop())
    mt5_client = MT5Client()
    connected = mt5_client.connect()
    if not connected:
        logger.error("MT5 failed to connect at startup — check credentials and MT5 terminal.")
    else:
        logger.info("MT5 connected at startup.")
        order_manager = OrderManager(mt5_client)
        _risk_manager = RiskManager()
        # Wire the SignalBus so approve → execute works
        bus.init(mt5_client, order_manager)
        # Start the strategy runner background loop (passes the same instance)
        start_runner_loop(mt5_client, order_manager, _risk_manager)
    yield
    # Shutdown
    if mt5_client:
        mt5_client.disconnect()
    logger.info("API shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="EVOTRADE-AI API",
    version="1.0.0",
    description="Trading bot backend — XM MT5 via Python",
    lifespan=lifespan,
)

# CORS — allow the Next.js dashboard (localhost:3000) during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(account.router,     prefix="/account",  tags=["Account"])
app.include_router(trades.router,      prefix="/trades",   tags=["Trades"])
app.include_router(signals.router,     prefix="/signals",  tags=["Signals"])
app.include_router(config.router,      prefix="/config",   tags=["Config"])
app.include_router(ai_routes.router,   prefix="/ai",       tags=["AI"])
app.include_router(risk_routes.router,     prefix="/risk",      tags=["Risk"])
app.include_router(backtest_routes.router, prefix="/backtest",  tags=["Backtest"])
app.include_router(ws_router,              prefix="/ws",        tags=["WebSocket"])


@app.get("/health", tags=["Health"])
def health():
    """Quick liveness check."""
    connected = mt5_client.is_connected() if mt5_client else False
    return {"status": "ok", "mt5_connected": connected}


@app.get("/logs/tail", tags=["Logs"])
def log_tail(n: int = 200):
    """Return the last N lines from the API log file for the dashboard log console."""
    if not _LOG_FILE.exists():
        return {"lines": []}
    text = _LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"lines": lines[-n:]}
