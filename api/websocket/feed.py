"""
WebSocket live price feed.
Clients connect to /ws/feed?symbols=EURUSD,GBPUSD,GOLD
and receive real-time tick updates at ~1 second intervals.
Also broadcasts open position updates so the dashboard stays in sync.
"""

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from loguru import logger

router = APIRouter()


class ConnectionManager:
    """
    Tracks all active WebSocket connections.
    Each connection is mapped to a set of symbols it subscribed to.
    """

    def __init__(self):
        # websocket → set of subscribed symbols
        self._connections: dict[WebSocket, set[str]] = {}

    async def connect(self, ws: WebSocket, symbols: set[str]) -> None:
        await ws.accept()
        self._connections[ws] = symbols
        logger.info(f"WS connected | symbols: {symbols} | total: {len(self._connections)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)
        logger.info(f"WS disconnected | remaining: {len(self._connections)}")

    async def broadcast_tick(self, symbol: str, payload: dict) -> None:
        """Send a tick update to all clients subscribed to this symbol."""
        dead = []
        for ws, syms in list(self._connections.items()):
            if symbol in syms:
                try:
                    await ws.send_text(json.dumps(payload))
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_positions(self, positions: list[dict]) -> None:
        """Send open positions snapshot to ALL connected clients."""
        if not self._connections:
            return
        payload = json.dumps({"type": "positions", "data": positions})
        dead = []
        for ws in list(self._connections.keys()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def broadcast_alert(self, alert: dict) -> None:
        """Send an alert (e.g. circuit_breaker) to ALL connected clients."""
        if not self._connections:
            return
        payload = json.dumps(alert)
        dead = []
        for ws in list(self._connections.keys()):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def active_symbols(self) -> set[str]:
        """All symbols currently subscribed to across all connections."""
        result: set[str] = set()
        for syms in self._connections.values():
            result |= syms
        return result

    @property
    def has_clients(self) -> bool:
        return bool(self._connections)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Background tick poller — runs once, polls MT5, broadcasts to all subscribers
# ---------------------------------------------------------------------------

_poller_task: Optional[asyncio.Task] = None

# Symbols that have had no tick data are suppressed after a few failures.
# Logged once on first failure, then silenced until a tick arrives again.
_no_tick_counts: dict[str, int] = {}
_no_tick_logged:  set[str]       = set()   # symbols already warned once
_NO_TICK_SUPPRESS_AFTER = 3  # suppress after 3 failed polls
_tick_counter: int = 0       # module-level counter for position broadcast throttle


async def _tick_poller():
    """
    Poll MT5 for current prices on all subscribed symbols every second.
    Broadcasts ticks and position updates to all connected WebSocket clients.
    """
    from api.main import get_mt5_client

    while True:
        await asyncio.sleep(1)

        if not manager.has_clients:
            continue

        client = get_mt5_client()
        if client is None or not client.is_connected():
            continue

        symbols = manager.active_symbols
        for symbol in symbols:
            # Skip symbols that have been consistently returning no tick
            if _no_tick_counts.get(symbol, 0) >= _NO_TICK_SUPPRESS_AFTER:
                continue
            tick = client.get_current_price(symbol)
            if tick is None:
                count = _no_tick_counts.get(symbol, 0) + 1
                _no_tick_counts[symbol] = count
                if symbol not in _no_tick_logged:
                    _no_tick_logged.add(symbol)
                    logger.debug(f"No tick data for {symbol} — will suppress further polls")
                continue
            # Tick arrived — reset suppress counter and re-enable logging
            _no_tick_counts[symbol] = 0
            _no_tick_logged.discard(symbol)
            payload = {
                "type":   "tick",
                "symbol": tick["symbol"],
                "bid":    tick["bid"],
                "ask":    tick["ask"],
                "time":   tick["time"].isoformat(),
            }
            await manager.broadcast_tick(symbol, payload)

        # Broadcast open positions every 5 seconds (not every tick)
        global _tick_counter
        _tick_counter += 1
        if _tick_counter % 5 == 0:
            positions = client.get_open_positions()
            # Serialise datetime objects
            for p in positions:
                if hasattr(p.get("open_time"), "isoformat"):
                    p["open_time"] = p["open_time"].isoformat()
            await manager.broadcast_positions(positions)

_monitor_task: Optional[asyncio.Task] = None
_last_alert_ts: dict[str, float] = {}  # alert_key → last sent timestamp
_ALERT_COOLDOWN_SECS = 3600  # don't repeat same alert within 1 hour


async def _performance_monitor():
    """
    Background task — checks trading performance every 5 minutes and
    broadcasts alerts to the dashboard when thresholds are breached:
      - Win rate drops below 40% for 10+ consecutive trades
      - A symbol is consistently losing (3+ losses, 0 wins)
      - RL agent risk factor drops below 0.6 (agent losing confidence)
      - LSTM degraded symbols detected
    """
    import time as _time
    while True:
        await asyncio.sleep(300)  # check every 5 minutes
        if not manager.has_clients:
            continue
        try:
            from ai.trade_memory import memory
            from ai.rl_agent import rl_manager

            now_ts = _time.monotonic()

            # ── Alert 1: overall win rate < 40% with 10+ recent trades ──────
            for trading_type in ("scalping", "day_trading", "swing"):
                stats = memory.stats(trading_type=trading_type, live_only=True)
                total = stats.get("total", 0)
                wr    = stats.get("win_rate", 1.0)
                if total >= 10 and wr < 0.40:
                    _key = f"low_winrate_{trading_type}"
                    if now_ts - _last_alert_ts.get(_key, 0) > _ALERT_COOLDOWN_SECS:
                        _last_alert_ts[_key] = now_ts
                        await broadcast_performance_alert({
                            "level":    "warning",
                            "category": "win_rate",
                            "message":  f"{trading_type.replace('_',' ').title()} win rate critical: {wr:.0%} over last {total} trades",
                            "trading_type": trading_type,
                            "win_rate": wr,
                            "total":    total,
                        })
                        logger.warning(f"Performance alert: {trading_type} win rate {wr:.0%} ({total} trades)")

            # ── Alert 2: RL risk factor < 0.6 (agent losing confidence) ─────
            rl_status = rl_manager.status()
            for trading_type, agent_status in rl_status.items():
                rf = agent_status.get("risk_factor", 1.0)
                ct = agent_status.get("confidence_threshold", 0.55)
                if rf < 0.60:
                    _key = f"low_rf_{trading_type}"
                    if now_ts - _last_alert_ts.get(_key, 0) > _ALERT_COOLDOWN_SECS:
                        _last_alert_ts[_key] = now_ts
                        await broadcast_performance_alert({
                            "level":    "info",
                            "category": "rl_agent",
                            "message":  f"RL agent [{trading_type}] reducing position sizes: risk_factor={rf:.2f}",
                            "trading_type": trading_type,
                            "risk_factor": rf,
                            "confidence_threshold": ct,
                        })

            # ── Alert 3: LSTM degraded symbols ───────────────────────────────
            lstm_acc = memory.lstm_accuracy(min_samples=15, live_only=True)
            degraded = lstm_acc.get("degraded_symbols", [])
            if degraded:
                _key = "degraded_lstm"
                if now_ts - _last_alert_ts.get(_key, 0) > _ALERT_COOLDOWN_SECS:
                    _last_alert_ts[_key] = now_ts
                    await broadcast_performance_alert({
                        "level":    "warning",
                        "category": "lstm_accuracy",
                        "message":  f"LSTM performing below random on: {', '.join(degraded)}",
                        "degraded_symbols": degraded,
                    })
            # ── Alert 3b: drift detection per trading type ───────────────────
            for trading_type in ("scalping", "day_trading", "swing"):
                drift = memory.detect_drift(trading_type=trading_type, window=30)
                if drift.get("drift_detected") and drift.get("severity") in ("medium", "high"):
                    _key = f"drift_{trading_type}"
                    if now_ts - _last_alert_ts.get(_key, 0) > _ALERT_COOLDOWN_SECS:
                        _last_alert_ts[_key] = now_ts
                        await broadcast_performance_alert({
                            "level":            "warning",
                            "category":         "drift",
                            "message":          (
                                f"{trading_type.replace('_',' ').title()} performance drifting: "
                                f"WR {drift['baseline_win_rate']:.0%} → {drift['recent_win_rate']:.0%}"
                            ),
                            "trading_type":     trading_type,
                            "severity":         drift["severity"],
                            "baseline_win_rate": drift["baseline_win_rate"],
                            "recent_win_rate":  drift["recent_win_rate"],
                        })

            # ── Alert 3c: live accuracy vs anchor baseline ───────────────────
            # Compare current LSTM live prediction accuracy against the anchor model's
            # training accuracy. If live accuracy has dropped >10 points below anchor,
            # the model may have drifted significantly from its baseline quality.
            try:
                from ai.predictor import predictor as _pred
                from pathlib import Path as _Path
                _models_dir = _Path("ai/models")
                for _anchor_meta_f in _models_dir.glob("*_anchor_meta.json"):
                    try:
                        _ameta = json.loads(_anchor_meta_f.read_text())
                        _anchor_acc = float(_ameta.get("accuracy", 0))
                        _key = _anchor_meta_f.stem.replace("_anchor_meta", "")
                        _live_meta_f = _models_dir / f"{_key}_meta.json"
                        if _live_meta_f.exists():
                            _lmeta = json.loads(_live_meta_f.read_text())
                            _live_acc = float(_lmeta.get("accuracy", 0))
                            _drop = _anchor_acc - _live_acc
                            if _drop > 0.08:  # 8+ point accuracy drop
                                _akey = f"anchor_drop_{_key}"
                                if now_ts - _last_alert_ts.get(_akey, 0) > _ALERT_COOLDOWN_SECS * 4:
                                    _last_alert_ts[_akey] = now_ts
                                    await broadcast_performance_alert({
                                        "level":        "warning",
                                        "category":     "model_degradation",
                                        "message":      f"LSTM {_key}: accuracy dropped from anchor {_anchor_acc:.1%} → {_live_acc:.1%}",
                                        "model_key":    _key,
                                        "anchor_acc":   _anchor_acc,
                                        "live_acc":     _live_acc,
                                        "accuracy_drop": round(_drop, 4),
                                    })
                    except Exception:
                        continue
            except Exception as _anc_exc:
                logger.debug(f"Anchor comparison error: {_anc_exc}")

            # ── Alert 4: symbol consistently losing ──────────────────────────
            recent = memory.recent(n=50, live_only=True)
            sym_pnl: dict[str, list[float]] = {}
            for o in recent:
                sym = o.get("symbol", "")
                if sym:
                    sym_pnl.setdefault(sym, []).append(o.get("profit", 0))
            for sym, pnls in sym_pnl.items():
                if len(pnls) >= 3 and all(p < 0 for p in pnls[-3:]):
                    _key = f"losing_sym_{sym}"
                    if now_ts - _last_alert_ts.get(_key, 0) > _ALERT_COOLDOWN_SECS:
                        _last_alert_ts[_key] = now_ts
                        await broadcast_performance_alert({
                            "level":    "warning",
                            "category": "symbol_performance",
                            "message":  f"{sym} has 3+ consecutive losses — consider disabling",
                            "symbol":   sym,
                            "recent_losses": len([p for p in pnls if p < 0]),
                        })

        except Exception as _exc:
            logger.debug(f"Performance monitor error: {_exc}")


def start_performance_monitor():
    """Start the performance monitor background task."""
    global _monitor_task
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_performance_monitor())
        logger.info("Performance monitor started.")

def start_poller():
    """Start the background tick poller if not already running."""
    global _poller_task
    if _poller_task is None or _poller_task.done():
        _poller_task = asyncio.create_task(_tick_poller())
        logger.info("Tick poller started.")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/feed")
async def websocket_feed(
    ws: WebSocket,
    symbols: str = Query(..., description="Comma-separated symbol list, e.g. EURUSD,GOLD"),
):
    """
    Subscribe to live price ticks for one or more symbols.

    Connect: ws://localhost:8000/ws/feed?symbols=EURUSD,GBPUSD,GOLD

    Message types received:
        { "type": "tick",      "symbol": "EURUSD", "bid": 1.1521, "ask": 1.1523, "time": "..." }
        { "type": "positions", "data": [ { ...position fields... } ] }
        { "type": "signal",    ...signal fields... }
    """
    symbol_set = {s.strip() for s in symbols.split(",") if s.strip()}

    await manager.connect(ws, symbol_set)
    start_poller()  # idempotent — only starts once
    start_performance_monitor()
    # Send an immediate one-time snapshot so the client has data before the first poll
    try:
        from api.main import get_mt5_client
        client = get_mt5_client()
        if client and client.is_connected():
            for symbol in symbol_set:
                tick = client.get_current_price(symbol)
                if tick:
                    await ws.send_text(json.dumps({
                        "type":   "tick",
                        "symbol": tick["symbol"],
                        "bid":    tick["bid"],
                        "ask":    tick["ask"],
                        "time":   tick["time"].isoformat(),
                    }))
    except Exception as e:
        logger.warning(f"Initial snapshot failed: {e}")

    # Keep connection alive — echo pings, disconnect on error
    try:
        while True:
            data = await ws.receive_text()
            # Support client-side symbol subscription updates
            try:
                msg = json.loads(data)
                if msg.get("type") == "subscribe" and "symbols" in msg:
                    new_syms = {s.strip() for s in msg["symbols"]}
                    manager._connections[ws] = new_syms
                    logger.debug(f"WS subscription updated: {new_syms}")
                elif msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Signal broadcast — called by the strategy runner to push signals to UI
# ---------------------------------------------------------------------------
async def broadcast_performance_alert(alert: dict) -> None:
    """Push a performance alert to all connected WebSocket clients."""
    payload = json.dumps({"type": "performance_alert", **alert})
    dead = []
    for ws in list(manager._connections.keys()):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        manager.disconnect(ws)

async def broadcast_signal(signal: dict) -> None:
    """Push a new trading signal to all connected WebSocket clients."""
    payload = json.dumps({"type": "signal", **signal})
    dead = []
    for ws in list(manager._connections.keys()):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        manager.disconnect(ws)
