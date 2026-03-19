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
        for ws, syms in self._connections.items():
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
            tick = client.get_current_price(symbol)
            if tick is None:
                continue
            payload = {
                "type":   "tick",
                "symbol": tick["symbol"],
                "bid":    tick["bid"],
                "ask":    tick["ask"],
                "time":   tick["time"].isoformat(),
            }
            await manager.broadcast_tick(symbol, payload)

        # Broadcast open positions every 5 seconds (not every tick)
        # Use a counter stored in the task itself
        counter = getattr(_tick_poller, "_counter", 0) + 1
        _tick_poller._counter = counter
        if counter % 5 == 0:
            positions = client.get_open_positions()
            # Serialise datetime objects
            for p in positions:
                if hasattr(p.get("open_time"), "isoformat"):
                    p["open_time"] = p["open_time"].isoformat()
            await manager.broadcast_positions(positions)


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
