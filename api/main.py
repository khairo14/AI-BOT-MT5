"""
FastAPI application entry point — Phase 3.
Mounts all REST routes and the WebSocket live feed.
"""

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Load environment variables before any imports that use them
load_dotenv()

from api.routes import account, trades, signals, config, ai as ai_routes, risk as risk_routes, backtest as backtest_routes, analytics as analytics_routes, scanner as scanner_routes, profitability as profitability_routes, auth as auth_routes, mt5_accounts as mt5_accounts_routes, notifications as notifications_routes, execution_quality as execution_quality_routes, portfolio as portfolio_routes, prometheus as prometheus_routes
from api.websocket.feed import router as ws_router
from api.signal_bus import bus, _set_event_loop
from api.runner_loop import start_runner_loop
from api.dependencies import verify_api_key
from api.routes import maintenance as maintenance_routes
from api.routes.signal_journal import router as signal_journal_router
from engine.mt5_client import MT5Client
from engine.order_manager import OrderManager
from engine.risk_manager import RiskManager
from ai.signal_validator import signal_validator


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
            for _key, agent in _rl._agents.items():
                # Use agent.trading_type (e.g. "scalping"), not the dict key
                # (e.g. "ema_scalp_scalping"), so per-mode thresholds apply correctly.
                _agent_type = agent.trading_type
                _last_ts = getattr(agent, "_last_update_ts", _now_ts)
                _hours_idle = (_now_ts - _last_ts) / 3600
                _threshold = _IDLE_HOURS.get(_agent_type, 6.0)
                if _hours_idle >= _threshold and agent._conf_thresh > DEFAULT_CONF_THRESH:
                    agent._conf_thresh = max(
                        DEFAULT_CONF_THRESH,
                        agent._conf_thresh - CONF_STEP
                    )
                    agent._last_update_ts = _now_ts
                    agent._force_save()
                    logger.info(
                        f"RL idle decay [{agent.strategy_name}/{_agent_type}]: "
                        f"conf_thresh → {agent._conf_thresh:.2f} "
                        f"(idle {_hours_idle:.1f}h)"
                    )
        except Exception as _exc:
            logger.debug(f"RL idle decay loop error: {_exc}")

_SCANNER_JSON_PATH = Path("config/scanner.json")
_MARKET_SCANNER_CFG_PATH = Path("config/market_scanner.json")

# Mode key mapping: scan summary key -> scanner.json key (they are the same, but explicit)
_SCAN_MODE_MAP = {
    "scalping":    {"timeframe": "M1/M5"},
    "day_trading": {"timeframe": "M15/H1"},
    "swing":       {"timeframe": "H4/D1"},
}


def _update_scanner_json(summary) -> None:
    """
    Write the top-ranked symbols from an auto-scan back to scanner.json so that
    runner_loop picks them up on the next bar.  Preserves the enabled flag for
    each mode and only replaces the symbols list.  Skipped if
    auto_update_scanner is false in market_scanner.json.
    """
    try:
        with open(_MARKET_SCANNER_CFG_PATH, "r", encoding="utf-8") as _f:
            scan_cfg = json.load(_f)
    except Exception as exc:
        logger.warning(f"Market scanner write-back: cannot read market_scanner.json: {exc}")
        return

    if not scan_cfg.get("auto_update_scanner", False):
        return

    max_per_mode: dict = scan_cfg.get("max_symbols_per_mode", {})

    # Load existing scanner.json to preserve the enabled flags and timeframe strings
    existing: dict = {}
    try:
        if _SCANNER_JSON_PATH.exists():
            existing = json.loads(_SCANNER_JSON_PATH.read_text(encoding="utf-8-sig"))
    except Exception:
        pass

    updated_any = False
    for mode, defaults in _SCAN_MODE_MAP.items():
        results = summary.trading_types.get(mode, [])
        if not results:
            continue

        mode_entry = existing.get(mode, {})

        # Respect manual_override: if the user has set this flag in scanner.json,
        # the auto-scan result is logged but the symbols list is NOT replaced.
        # Set manual_override=false in scanner.json to re-enable auto-update.
        if mode_entry.get("manual_override", False):
            logger.info(
                f"Market scanner write-back [{mode}]: manual_override=true — "
                f"keeping existing symbols, ignoring {len(results)} scan results"
            )
            continue

        limit = max_per_mode.get(mode, 15)
        top_symbols = [r.symbol for r in results[:limit]]

        old_symbols = mode_entry.get("symbols", [])

        existing[mode] = {
            "enabled":         mode_entry.get("enabled", True),
            "manual_override": mode_entry.get("manual_override", False),
            "symbols":         top_symbols,
            "timeframe":       mode_entry.get("timeframe", defaults["timeframe"]),
        }

        logger.info(
            f"Market scanner write-back [{mode}]: "
            f"{len(old_symbols)} → {len(top_symbols)} symbols | {top_symbols}"
        )
        updated_any = True

    if not updated_any:
        return

    # Atomic write: write to .tmp then rename so runner_loop never reads a partial file
    try:
        tmp = _SCANNER_JSON_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp.replace(_SCANNER_JSON_PATH)
        logger.info("Market scanner write-back: scanner.json updated")
    except Exception as exc:
        logger.warning(f"Market scanner write-back: failed to write scanner.json: {exc}")


async def _market_scanner_loop() -> None:
    """
    Background task that runs market scanner every 60 minutes.
    Discovers new trading opportunities and updates scanner.json via write-back.
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

            # Write top symbols back to scanner.json so runner_loop uses them next bar
            _update_scanner_json(summary)

        except Exception as _exc:
            logger.warning(f"Market scanner loop error: {_exc}")

        # Wait 60 minutes before next scan
        await asyncio.sleep(3600)

async def _signal_validation_loop() -> None:
    """
    Background task that validates old enough signal-journal entries.

    Runs every 15 minutes.
    Validation is analytics-only and does NOT affect RL/LSTM learning.
    """
    await asyncio.sleep(180)

    while True:
        try:
            if mt5_client is None or not mt5_client.is_connected():
                logger.debug("Signal validator: MT5 not connected, skipping cycle")
                await asyncio.sleep(300)
                continue

            result = signal_validator.validate_pending_signals(
                max_signals=100,
                client=mt5_client,
            )

            checked = int(result.get("checked", 0))
            validated = int(result.get("validated", 0))

            if checked > 0 or validated > 0:
                logger.info(
                    f"Signal validator: "
                    f"checked={checked} | "
                    f"validated={validated} | "
                    f"pending_seen={result.get('pending_seen', 0)}"
                )

        except Exception as exc:
            logger.exception(f"Signal validator loop failed: {exc}")

        await asyncio.sleep(15 * 60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mt5_client, _risk_manager
    logger.info("Starting EVOTRADE-AI API...")
    # Register the running event loop so thread executors can schedule coroutines safely
    _set_event_loop(asyncio.get_running_loop())
    mt5_client = MT5Client()
    app.state.mt5_client = mt5_client
    connected = mt5_client.connect()
    if not connected:
        logger.error("MT5 failed to connect at startup — check credentials and MT5 terminal.")
    else:
        logger.info("MT5 connected at startup.")
        order_manager = OrderManager(mt5_client)
        _risk_manager = RiskManager()

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
        setattr(_risk_manager, "_on_circuit_breaker", _on_cb)
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
        # Recover close events for any trades that closed while server was offline
        try:
            from api.signal_bus import recover_unclosed_trades
            await recover_unclosed_trades(mt5_client)
            start_runner_loop(mt5_client, order_manager, _risk_manager)
            logger.info("Startup validation complete | ""trade_state loaded | ""recovery complete | ""runner enabled")
        except Exception as _rec_exc:
            logger.warning(f"Startup recovery task failed to launch: {_rec_exc}")
        try:
            asyncio.create_task(bus.restore_pending_swing_signals())
        except Exception as _pss_exc:
            logger.warning(f"Pending swing signals restore failed: {_pss_exc}")
        # G-1: MT5 watchdog — reconnect automatically if the terminal drops
        asyncio.create_task(_mt5_watchdog())
        # RL idle decay — runs every 30 min to decay thresholds during no-trade periods
        asyncio.create_task(_rl_idle_decay_loop())
        # Market scanner — runs every 60 min to discover new trading opportunities
        asyncio.create_task(_market_scanner_loop())
        # signal validation loop
        asyncio.create_task(_signal_validation_loop())
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

from starlette.requests import Request as StarletteRequest
async def rate_limit_handler(request: StarletteRequest, exc: Exception):
    return _rate_limit_exceeded_handler(request, exc)  # type: ignore[arg-type]
# Rate limiting state and error handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)


@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    """Attach a correlation ID to every request for log tracing.
    Also records request count, latency, and errors for Prometheus metrics."""
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    _t0 = time.time()
    with logger.contextualize(correlation_id=correlation_id):
        response = await call_next(request)
    _duration_ms = (time.time() - _t0) * 1000
    # Feed Prometheus tracking — skip the /metrics endpoint itself to avoid recursion
    _path = request.url.path
    if _path != "/metrics":
        try:
            from api.routes.prometheus import (
                increment_request_count,
                record_request_duration,
                increment_error_count,
            )
            increment_request_count(_path)
            record_request_duration(_path, _duration_ms)
            if response.status_code >= 500:
                increment_error_count(_path)
        except Exception:
            pass
    response.headers["X-Correlation-ID"] = correlation_id
    return response

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
app.include_router(auth_routes.router, prefix="/auth", tags=["Authentication"])
app.include_router(mt5_accounts_routes.router, prefix="/api", tags=["MT5 Accounts"])
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
app.include_router(notifications_routes.router, prefix="/notifications", tags=["Notifications"])
app.include_router(execution_quality_routes.router, prefix="/execution-quality", tags=["Execution Quality"])
app.include_router(portfolio_routes.router, prefix="/portfolio", tags=["Portfolio Optimization"])
app.include_router(prometheus_routes.router, prefix="/metrics", tags=["Prometheus Metrics"])
app.include_router(ws_router, prefix="/ws", tags=["WebSocket"])
app.include_router(maintenance_routes.router)
app.include_router(signal_journal_router)

# Internal endpoint for optimizer completion notification
@app.post("/internal/optimizer-complete", tags=["Internal"])
async def optimizer_complete_notification(request: Request):
    """Receive optimizer completion notification and broadcast via WebSocket."""
    try:
        data = await request.json()
        from api.websocket.feed import manager as _ws_manager
        await _ws_manager.broadcast_alert({
            "type": "optimizer_complete",
            "completed": data.get("completed", 0),
            "best_score": data.get("best_score", 0.0),
        })
        return {"status": "ok"}
    except Exception as exc:
        logger.error(f"Optimizer complete notification failed: {exc}")
        return {"status": "error", "message": str(exc)}


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
            circuit_breaker_active = any(
                bool(getattr(_risk_manager, attr, False))
                for attr in (
                    "_daily_breaker_active",
                    "_weekly_breaker_active",
                    "_monthly_breaker_active",
                )
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