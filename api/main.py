"""
FastAPI application entry point — Phase 3.
Mounts all REST routes and the WebSocket live feed.
"""

import asyncio
import json
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from api.routes import account, trades, signals, config, ai as ai_routes, risk as risk_routes, backtest as backtest_routes, analytics as analytics_routes, scanner as scanner_routes, profitability as profitability_routes
from api.websocket.feed import router as ws_router
from api.signal_bus import bus, _set_event_loop
from api.runner_loop import start_runner_loop
from api.dependencies import verify_api_key
from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager

# ---------------------------------------------------------------------------
# Logging setup (Task #11: Logging Improvements)
# ---------------------------------------------------------------------------
_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

# Environment settings
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if ENVIRONMENT == "development" else "INFO")

logger.remove()  # drop default stderr sink

# 1. Console logging (human-readable, colored)
logger.add(
    sys.stderr,
    level=LOG_LEVEL,
    colorize=True,
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> | <level>{message}</level>",
    backtrace=True,
    diagnose=True,
)

# 2. File logging (detailed, daily rotation)
logger.add(
    str(_LOG_DIR / "api_{time:YYYY-MM-DD}.log"),
    level="DEBUG",
    rotation="00:00",  # Daily rotation at midnight
    retention="7 days",  # Keep 7 days
    compression="zip",  # Compress old logs
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    backtrace=True,
    diagnose=True,
)

# 3. JSON logging (production only, for log aggregation)
if ENVIRONMENT == "production":
    logger.add(
        str(_LOG_DIR / "api_{time:YYYY-MM-DD}.json"),
        level="INFO",
        rotation="00:00",
        retention="30 days",  # Keep JSON logs longer
        compression="zip",
        encoding="utf-8",
        serialize=True,  # Built-in JSON serialization
    )
    logger.info("JSON logging enabled for production environment")

# 4. Error-only log (critical issues)
logger.add(
    str(_LOG_DIR / "errors_{time:YYYY-MM-DD}.log"),
    level="ERROR",
    rotation="00:00",
    retention="30 days",  # Keep errors longer
    compression="zip",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
    backtrace=True,
    diagnose=True,
)

logger.info(f"Logging initialized | Environment: {ENVIRONMENT} | Level: {LOG_LEVEL}")


def _get_latest_api_log_file() -> Path | None:
    """Return the most recent daily API log file for dashboard log tailing."""
    candidates = sorted(_LOG_DIR.glob("api_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None

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
# Rate Limiting (Task #7)
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

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

# Rate limiting state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    """Attach a correlation ID to every request for log tracing."""
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    with logger.contextualize(correlation_id=correlation_id):
        response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(account.router,     prefix="/account",  tags=["Account"])
app.include_router(trades.router,      prefix="/trades",   tags=["Trades"])
app.include_router(signals.router,     prefix="/signals",  tags=["Signals"])
app.include_router(config.router,      prefix="/config",   tags=["Config"])
app.include_router(ai_routes.router,   prefix="/ai",       tags=["AI"])
app.include_router(risk_routes.router, prefix="/risk", tags=["Risk"])
app.include_router(backtest_routes.router, prefix="/backtest", tags=["Backtest"])
app.include_router(analytics_routes.router, prefix="/analytics", tags=["Analytics"])
app.include_router(scanner_routes.router, prefix="/scanner", tags=["Scanner"])
app.include_router(profitability_routes.router, prefix="/profitability", tags=["Profitability"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])


@app.get("/health", tags=["Health"])
@limiter.limit("60/minute")
def health(request: Request):
    """Enhanced health check endpoint for uptime monitoring."""
    import platform
    import psutil
    from fastapi import status

    mt5_connected = False
    mt5_status = "disconnected"
    try:
        if mt5_client:
            mt5_connected = mt5_client.is_connected()
            mt5_status = "ok" if mt5_connected else "disconnected"
    except Exception as exc:
        mt5_status = f"error: {exc}"

    disk_status = "ok"
    disk_free_gb = 0.0
    disk_usage_pct = 0.0
    try:
        disk_root = "d:\\" if platform.system() == "Windows" else "/"
        disk = psutil.disk_usage(disk_root)
        disk_free_gb = disk.free / (1024 ** 3)
        disk_usage_pct = disk.percent
        if disk_free_gb < 5.0 or disk_usage_pct > 90:
            disk_status = "warning"
    except Exception as exc:
        disk_status = f"error: {exc}"

    memory_status = "ok"
    memory_usage_pct = 0.0
    memory_available_gb = 0.0
    try:
        mem = psutil.virtual_memory()
        memory_usage_pct = mem.percent
        memory_available_gb = mem.available / (1024 ** 3)
        if memory_usage_pct > 90 or memory_available_gb < 1.0:
            memory_status = "warning"
    except Exception as exc:
        memory_status = f"error: {exc}"

    risk_status = "ok" if _risk_manager else "not_initialized"
    circuit_breaker_active = False
    if _risk_manager is not None:
        try:
            circuit_breaker_active = (
                _risk_manager._daily_breaker_active
                or _risk_manager._weekly_breaker_active
                or _risk_manager._monthly_breaker_active
            )
            if circuit_breaker_active:
                risk_status = "circuit_breaker_active"
        except Exception:
            risk_status = "ok"

    checks = {
        "mt5_connection": mt5_status,
        "disk_space": disk_status,
        "memory": memory_status,
        "risk_manager": risk_status,
    }
    critical_issues = [
        key for key, value in checks.items()
        if value not in ("ok", "warning", "circuit_breaker_active", "not_initialized")
    ]
    is_healthy = len(critical_issues) == 0 and mt5_connected
    payload = {
        "status": "healthy" if is_healthy else "degraded",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "version": "1.0.0",
        "checks": checks,
        "metrics": {
            "disk_free_gb": round(disk_free_gb, 2),
            "disk_usage_pct": round(disk_usage_pct, 1),
            "memory_usage_pct": round(memory_usage_pct, 1),
            "memory_available_gb": round(memory_available_gb, 2),
            "circuit_breaker_active": circuit_breaker_active,
        },
        "trading_mode": mt5_client.trading_mode if mt5_client else "unknown",
        "correlation_id": getattr(request.state, "correlation_id", None),
    }
    status_code = status.HTTP_200_OK if is_healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    return Response(content=json.dumps(payload), status_code=status_code, media_type="application/json")


@app.get("/logs/tail", tags=["Logs"])
@limiter.limit("30/minute")
def log_tail(request: Request, n: int = 200):
    """Return the last N lines from the most recent API log file."""
    log_file = _get_latest_api_log_file()
    if log_file is None or not log_file.exists():
        return {"lines": []}
    text = log_file.read_text(encoding="utf-8", errors="ignore")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return {"lines": lines[-n:]}


@app.get("/rate-limit-status", tags=["Health"])
def rate_limit_status(request: Request):
    """Return rate limiting configuration and status."""
    return {
        "rate_limiting_enabled": True,
        "global_limit": "200 requests/minute",
        "limits": {
            "signals": "100/minute",
            "scanner": "30/minute (full scan), 60/minute (results)",
            "ai_predictions": "60/minute",
            "trades": "100/minute",
            "health": "60/minute",
            "logs": "30/minute"
        },
        "client_ip": get_remote_address(request),
        "task": "Task #7: Rate Limiting",
        "correlation_id": getattr(request.state, "correlation_id", None),
    }