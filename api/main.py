"""
FastAPI application entry point — Phase 3.
Mounts all REST routes and the WebSocket live feed.
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from api.routes import account, trades, signals, config, ai as ai_routes, risk as risk_routes, backtest as backtest_routes, analytics as analytics_routes, scanner as scanner_routes
from api.websocket.feed import router as ws_router
from api.signal_bus import bus, _set_event_loop
from api.runner_loop import start_runner_loop
from api.dependencies import verify_api_key
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


async def _mt5_watchdog() -> None:
    """G-1: Periodically verify MT5 connection and reconnect if it dropped."""
    while True:
        await asyncio.sleep(30)
        if mt5_client is None:
            continue
        try:
            connected = await asyncio.to_thread(mt5_client.is_connected)
            if not connected:
                logger.warning("MT5 watchdog: connection lost — attempting reconnect...")
                ok = await asyncio.to_thread(mt5_client.reconnect)
                if ok:
                    logger.info("MT5 watchdog: reconnected successfully.")
                else:
                    logger.error("MT5 watchdog: reconnect failed — will retry in 30 s.")
        except Exception as _exc:
            logger.warning(f"MT5 watchdog error: {_exc}")

async def _rl_idle_decay_loop() -> None:
    """
    Background task that triggers RL agent idle decay every 30 minutes.
    Normally idle decay fires inside observe() on trade close, but during
    long periods with no trades (weekends, holidays) observe() never runs.
    This loop ensures thresholds still decay toward default over time.
    """
    while True:
        await asyncio.sleep(1800)  # check every 30 minutes
        try:
            from ai.rl_agent import rl_manager as _rl, DEFAULT_CONF_THRESH, CONF_STEP
            from datetime import datetime, timezone
            _now_ts = datetime.now(tz=timezone.utc).timestamp()
            # Per-mode idle thresholds — must match _IDLE_DECAY_HOURS in rl_agent.py
            # so this background loop behaves identically to the in-observe() decay.
            _IDLE_HOURS = {"scalping": 4.0, "day_trading": 6.0, "swing": 12.0}
            for trading_type, agent in _rl._agents.items():
                _last_ts = getattr(agent, "_last_update_ts", _now_ts)
                _hours_idle = (_now_ts - _last_ts) / 3600
                _threshold = _IDLE_HOURS.get(trading_type, 6.0)
                # Decay whenever above the hard floor (DEFAULT_CONF_THRESH = 0.55).
                # The old condition "> 0.55" excluded agents at exactly 0.55 — but
                # bootstrap can set values below 0.55 (e.g. scalping 0.54) that should
                # still decay upward toward 0.55 if they were manually set lower.
                # Use ">= floor of this mode" instead so decay always trends toward default.
                _CONF_FLOOR = {"scalping": 0.52, "day_trading": 0.52, "swing": 0.50}
                _floor = _CONF_FLOOR.get(trading_type, 0.52)
                if _hours_idle >= _threshold and agent._conf_thresh > DEFAULT_CONF_THRESH:
                    agent._conf_thresh = max(
                        DEFAULT_CONF_THRESH,
                        agent._conf_thresh - CONF_STEP
                    )
                    agent._last_update_ts = _now_ts
                    agent._force_save()
                    logger.info(
                        f"RL idle decay [{trading_type}]: "
                        f"conf_thresh → {agent._conf_thresh:.2f} "
                        f"(idle {_hours_idle:.1f}h)"
                    )
        except Exception as _exc:
            logger.debug(f"RL idle decay loop error: {_exc}")

async def _market_scanner_loop() -> None:
    """
    Background task that runs market scanner every 60 minutes.
    Discovers new trading opportunities and updates cached results.
    """
    # Wait 2 minutes after startup before first scan (let system stabilize)
    await asyncio.sleep(120)
    
    while True:
        try:
            if mt5_client is None or not mt5_client.is_connected():
                logger.debug("Market scanner: MT5 not connected, skipping scan")
                await asyncio.sleep(600)  # check again in 10 minutes
                continue
            
            logger.info("Market scanner: Starting automatic scan...")
            start_time = datetime.now(timezone.utc)
            
            from engine.market_scanner import MarketScanner
            scanner = MarketScanner(mt5_client)
            summary = await asyncio.to_thread(scanner.scan_all, force_refresh=True)
            
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            
            logger.info(
                f"Market scanner: Complete | "
                f"Scanned: {summary.total_scanned} | "
                f"Passed: {summary.total_passed} | "
                f"Duration: {duration:.1f}s | "
                f"Scalping: {len(summary.trading_types.get('scalping', []))} | "
                f"Day: {len(summary.trading_types.get('day_trading', []))} | "
                f"Swing: {len(summary.trading_types.get('swing', []))}"
            )
            
        except Exception as _exc:
            logger.warning(f"Market scanner loop error: {_exc}")
        
        # Wait 60 minutes before next scan
        await asyncio.sleep(3600)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mt5_client, _risk_manager
    logger.info("Starting EVOTRADE-AI API...")
    # Register the running event loop so thread executors can schedule coroutines safely
    _set_event_loop(asyncio.get_running_loop())
    mt5_client = MT5Client()
    connected = mt5_client.connect()
    if not connected:
        logger.error("MT5 failed to connect at startup — check credentials and MT5 terminal.")
    else:
        logger.info("MT5 connected at startup.")
        order_manager = OrderManager(mt5_client)
        _risk_manager = RiskManager()
        # Create paper trade engine singleton (used for sync_positions)
        import engine.paper_trade as _pt_mod
        _pt_mod.paper_engine = _pt_mod.PaperTradeEngine(
            client=mt5_client,
            order_manager=order_manager,
            risk_manager=_risk_manager,
        )
        # Wire the SignalBus so approve → execute works
        bus.init(mt5_client, order_manager)
        # G-3: wire circuit-breaker alert → WebSocket broadcast
        # Capture the running loop reference NOW (we are inside the lifespan coroutine,
        # so get_running_loop() is safe). The callback fires from a worker thread later,
        # so we must NOT call get_event_loop() inside it — that is not thread-safe.
        from api.websocket.feed import manager as _ws_manager
        _running_loop = asyncio.get_running_loop()
        def _on_cb(kind: str, message: str) -> None:
            asyncio.run_coroutine_threadsafe(
                _ws_manager.broadcast_alert({"type": "circuit_breaker", "kind": kind, "message": message}),
                _running_loop,
            )
        _risk_manager._on_circuit_breaker = _on_cb
        # GAP-1: pre-seed day/week start balance immediately so drawdown protection
        # is active from the very first trade, not only after update_balance() fires.
        try:
            _acct_seed = await asyncio.to_thread(mt5_client.get_account_info)
            if _acct_seed and _acct_seed.get("balance"):
                _risk_manager.update_balance(float(_acct_seed["balance"]))
                logger.info(f"RiskManager: seeded start balance = {_acct_seed['balance']:.2f}")
        except Exception as _seed_exc:
            logger.warning(f"RiskManager: balance seed failed: {_seed_exc}")
        # Start the strategy runner background loop (passes the same instance)
        start_runner_loop(mt5_client, order_manager, _risk_manager)
        # Recover close events for any trades that closed while server was offline
        try:
            from api.signal_bus import recover_unclosed_trades
            asyncio.create_task(recover_unclosed_trades(mt5_client))
        except Exception as _rec_exc:
            logger.warning(f"Startup recovery task failed to launch: {_rec_exc}")
        # G-1: MT5 watchdog — reconnect automatically if the terminal drops
        asyncio.create_task(_mt5_watchdog())
        # RL idle decay — runs every 30 min to decay thresholds during no-trade periods
        asyncio.create_task(_rl_idle_decay_loop())
        # Market scanner — runs every 60 min to discover new trading opportunities
        asyncio.create_task(_market_scanner_loop())
    yield
    # Shutdown
    try:
        from ai.rl_agent import rl_manager as _rl_manager
        _rl_manager.shutdown()
    except Exception as _rl_exc:
        logger.warning(f"RL shutdown save failed: {_rl_exc}")
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
    dependencies=[Depends(verify_api_key)],
)

# CORS — allow the Next.js dashboard during development; override via CORS_ORIGINS env var
_cors_origins = os.getenv(
    "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
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
app.include_router(analytics_routes.router, prefix="/analytics", tags=["Analytics"])
app.include_router(scanner_routes.router,   prefix="/scanner",   tags=["Scanner"])
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