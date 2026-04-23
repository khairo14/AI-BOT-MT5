"""
Prometheus Metrics Exporter — Task 28

Exposes system and trading metrics in Prometheus format for monitoring and alerting.

Metrics exposed:
- System: CPU usage, memory usage, disk space
- Trading: open positions, total PnL, drawdown, signal rate
- API: request count, latency, error rate
- MT5: connection status, tick updates, order success rate

Endpoint: GET /metrics

Grafana dashboards consume these metrics for visualization and alerts.
"""

from __future__ import annotations

import os
import psutil
import time
from collections import defaultdict
from pathlib import Path

from fastapi import APIRouter, Response
from loguru import logger
from engine.account_store import current_mode as _cur_mode

router = APIRouter()

# Track API metrics
_request_count = defaultdict(int)
_request_duration = defaultdict(list)
_error_count = defaultdict(int)
_last_reset = time.time()


def increment_request_count(endpoint: str):
    """Track API request count per endpoint."""
    _request_count[endpoint] += 1


def record_request_duration(endpoint: str, duration_ms: float):
    """Record API request duration."""
    _request_duration[endpoint].append(duration_ms)
    # Keep only last 100 requests to avoid memory leak
    if len(_request_duration[endpoint]) > 100:
        _request_duration[endpoint] = _request_duration[endpoint][-100:]


def increment_error_count(endpoint: str):
    """Track API error count per endpoint."""
    _error_count[endpoint] += 1


@router.get("")
def prometheus_metrics():
    """
    Return all metrics in Prometheus exposition format:
    
    # HELP metric_name Description of the metric
    # TYPE metric_name metric_type
    metric_name{label="value"} value timestamp
    """
    lines = []
    
    # ─── System Metrics ───────────────────────────────────────────────────
    
    # CPU usage
    cpu_percent = psutil.cpu_percent(interval=0.1)
    lines.append("# HELP aibot_cpu_usage_percent Current CPU usage percentage")
    lines.append("# TYPE aibot_cpu_usage_percent gauge")
    lines.append(f"aibot_cpu_usage_percent {cpu_percent}")
    
    # Memory usage
    mem = psutil.virtual_memory()
    lines.append("# HELP aibot_memory_usage_bytes Current memory usage in bytes")
    lines.append("# TYPE aibot_memory_usage_bytes gauge")
    lines.append(f"aibot_memory_usage_bytes {mem.used}")
    
    lines.append("# HELP aibot_memory_usage_percent Current memory usage percentage")
    lines.append("# TYPE aibot_memory_usage_percent gauge")
    lines.append(f"aibot_memory_usage_percent {mem.percent}")
    
    # Disk usage
    disk = psutil.disk_usage('.')
    lines.append("# HELP aibot_disk_usage_bytes Current disk usage in bytes")
    lines.append("# TYPE aibot_disk_usage_bytes gauge")
    lines.append(f"aibot_disk_usage_bytes {disk.used}")
    
    lines.append("# HELP aibot_disk_usage_percent Current disk usage percentage")
    lines.append("# TYPE aibot_disk_usage_percent gauge")
    lines.append(f"aibot_disk_usage_percent {disk.percent}")
    
    # Process info
    process = psutil.Process()
    lines.append("# HELP aibot_process_threads Number of threads in use")
    lines.append("# TYPE aibot_process_threads gauge")
    lines.append(f"aibot_process_threads {process.num_threads()}")
    
    # ─── Trading Metrics ─────────────────────────────────────────────────
    
    try:
        from api.main import get_mt5_client
        client = get_mt5_client()
        
        if client and client.is_connected():
            # Connection status
            lines.append("# HELP aibot_mt5_connected MT5 connection status (1=connected, 0=disconnected)")
            lines.append("# TYPE aibot_mt5_connected gauge")
            lines.append("aibot_mt5_connected 1")
            
            # Open positions
            positions = client.get_open_positions()
            lines.append("# HELP aibot_open_positions Number of open positions")
            lines.append("# TYPE aibot_open_positions gauge")
            lines.append(f"aibot_open_positions {len(positions)}")
            
            # Total PnL
            total_pnl = sum(p.get("profit", 0) for p in positions)
            lines.append("# HELP aibot_unrealized_pnl Total unrealized P&L from open positions")
            lines.append("# TYPE aibot_unrealized_pnl gauge")
            lines.append(f"aibot_unrealized_pnl {total_pnl}")
        else:
            lines.append("# HELP aibot_mt5_connected MT5 connection status (1=connected, 0=disconnected)")
            lines.append("# TYPE aibot_mt5_connected gauge")
            lines.append("aibot_mt5_connected 0")
    except Exception as e:
        logger.debug(f"MT5 metrics collection failed: {e}")
        lines.append("aibot_mt5_connected 0")
    
    # Signal queue size
    try:
        from api.signal_bus import bus
        queue_size = len(bus.queue)
        lines.append("# HELP aibot_signal_queue_size Number of signals in queue")
        lines.append("# TYPE aibot_signal_queue_size gauge")
        lines.append(f"aibot_signal_queue_size {queue_size}")
    except Exception as e:
        logger.debug(f"Signal queue metrics failed: {e}")
    
    # ─── API Metrics ──────────────────────────────────────────────────────
    
    # Total requests
    total_requests = sum(_request_count.values())
    lines.append("# HELP aibot_api_requests_total Total API requests received")
    lines.append("# TYPE aibot_api_requests_total counter")
    lines.append(f"aibot_api_requests_total {total_requests}")
    
    # Requests per endpoint
    lines.append("# HELP aibot_api_requests_by_endpoint Requests per endpoint")
    lines.append("# TYPE aibot_api_requests_by_endpoint counter")
    for endpoint, count in _request_count.items():
        safe_endpoint = endpoint.replace('"', '\\"')
        lines.append(f'aibot_api_requests_by_endpoint{{endpoint="{safe_endpoint}"}} {count}')
    
    # Average latency per endpoint
    lines.append("# HELP aibot_api_latency_ms Average API latency in milliseconds")
    lines.append("# TYPE aibot_api_latency_ms gauge")
    for endpoint, durations in _request_duration.items():
        if durations:
            avg_duration = sum(durations) / len(durations)
            safe_endpoint = endpoint.replace('"', '\\"')
            lines.append(f'aibot_api_latency_ms{{endpoint="{safe_endpoint}"}} {avg_duration:.2f}')
    
    # Total errors
    total_errors = sum(_error_count.values())
    lines.append("# HELP aibot_api_errors_total Total API errors")
    lines.append("# TYPE aibot_api_errors_total counter")
    lines.append(f"aibot_api_errors_total {total_errors}")
    
    # Errors per endpoint
    lines.append("# HELP aibot_api_errors_by_endpoint Errors per endpoint")
    lines.append("# TYPE aibot_api_errors_by_endpoint counter")
    for endpoint, count in _error_count.items():
        safe_endpoint = endpoint.replace('"', '\\"')
        lines.append(f'aibot_api_errors_by_endpoint{{endpoint="{safe_endpoint}"}} {count}')
    
    # Uptime
    uptime_seconds = time.time() - _last_reset
    lines.append("# HELP aibot_uptime_seconds Time since metrics reset")
    lines.append("# TYPE aibot_uptime_seconds counter")
    lines.append(f"aibot_uptime_seconds {int(uptime_seconds)}")
    
    # ─── Trade Journal Metrics ────────────────────────────────────────────
    
    try:
        from engine.trade_journal import trade_journal
        
        # Count trades today
        from datetime import datetime, timezone
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        
        all_trades = trade_journal.get(account=_cur_mode(), event="open", limit=1000)
        trades_today = [t for t in all_trades if t.get("logged_at", "")[:10] == today]
        
        lines.append("# HELP aibot_trades_today Number of trades opened today")
        lines.append("# TYPE aibot_trades_today gauge")
        lines.append(f"aibot_trades_today {len(trades_today)}")
        
        # Total trades (all time)
        lines.append("# HELP aibot_trades_total Total trades (last 1000)")
        lines.append("# TYPE aibot_trades_total counter")
        lines.append(f"aibot_trades_total {len(all_trades)}")
    except Exception as e:
        logger.debug(f"Trade journal metrics failed: {e}")
    
    # Return as plain text with Prometheus content type
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4"
    )


@router.post("/reset")
def reset_metrics():
    """Reset API metrics (for testing/debugging)."""
    _request_count.clear()
    _request_duration.clear()
    _error_count.clear()
    global _last_reset
    _last_reset = time.time()
    return {"status": "metrics_reset", "timestamp": _last_reset}
