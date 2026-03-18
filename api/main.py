"""
FastAPI application entry point — Phase 3.
Mounts all REST routes and the WebSocket live feed.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.routes import account, trades, signals, config
from api.websocket.feed import router as ws_router
from api.signal_bus import bus
from api.runner_loop import start_runner_loop
from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager

# ---------------------------------------------------------------------------
# Shared MT5 client — created once at startup, closed at shutdown
# ---------------------------------------------------------------------------
mt5_client: MT5Client | None = None


def get_mt5_client() -> MT5Client:
    """Return the application-level MT5 client instance."""
    return mt5_client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mt5_client
    logger.info("Starting AI-BOT-MT5 API...")
    mt5_client = MT5Client()
    connected = mt5_client.connect()
    if not connected:
        logger.error("MT5 failed to connect at startup — check credentials and MT5 terminal.")
    else:
        logger.info("MT5 connected at startup.")
        order_manager = OrderManager(mt5_client)
        risk_manager = RiskManager()
        # Wire the SignalBus so approve → execute works
        bus.init(mt5_client, order_manager)
        # Start the strategy runner background loop
        start_runner_loop(mt5_client, order_manager, risk_manager)
    yield
    # Shutdown
    if mt5_client:
        mt5_client.disconnect()
    logger.info("API shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="AI-BOT-MT5 API",
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
app.include_router(account.router,  prefix="/account",  tags=["Account"])
app.include_router(trades.router,   prefix="/trades",   tags=["Trades"])
app.include_router(signals.router,  prefix="/signals",  tags=["Signals"])
app.include_router(config.router,   prefix="/config",   tags=["Config"])
app.include_router(ws_router,       prefix="/ws",       tags=["WebSocket"])


@app.get("/health", tags=["Health"])
def health():
    """Quick liveness check."""
    connected = mt5_client.is_connected() if mt5_client else False
    return {"status": "ok", "mt5_connected": connected}
