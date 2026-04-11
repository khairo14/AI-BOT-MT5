# Grafana Dashboard Configuration — Task 28

This directory contains Grafana dashboard JSON templates for monitoring AI-BOT-MT5.

## Setup

1. **Install Prometheus** (scrape metrics from `/metrics` endpoint)
2. **Install Grafana** (visualize metrics)
3. **Import dashboards** (JSON files in this directory)

## Dashboard Files

### 1. `system-health.json` — System Monitoring
- CPU usage
- Memory usage
- Disk space
- Process threads
- Uptime

**Alerts:**
- CPU > 80% for 5 minutes
- Memory > 90% for 5 minutes
- Disk > 85%

### 2. `trading-performance.json` — Trading Metrics
- Open positions count
- Unrealized P&L
- Trades executed today
- Signal queue size
- Win rate

**Alerts:**
- Drawdown > 10%
- Signal queue backlog > 50
- No trades for 24 hours (when market is open)

### 3. `api-performance.json` — API Metrics
- Request rate (requests/sec)
- Latency percentiles (p50, p95, p99)
- Error rate
- Endpoint breakdown

**Alerts:**
- Error rate > 5%
- Latency p95 > 500ms
- Request rate spike (2x baseline)

### 4. `mt5-connection.json` — MT5 Status
- Connection uptime
- Tick update rate
- Order fill rate
- Execution time

**Alerts:**
- MT5 disconnected for > 1 minute
- Order fill rate < 90%
- Execution time > 1000ms

## Prometheus Configuration

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'aibot'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
```

## Alert Manager Rules

Create `alerts.yml`:

```yaml
groups:
  - name: aibot_alerts
    interval: 30s
    rules:
      - alert: HighCPU
        expr: aibot_cpu_usage_percent > 80
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High CPU usage detected"
          
      - alert: MT5Disconnected
        expr: aibot_mt5_connected == 0
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "MT5 connection lost"
          
      - alert: HighErrorRate
        expr: rate(aibot_api_errors_total[5m]) > 0.05
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "API error rate above 5%"
```

## Import Instructions

1. Open Grafana (default: http://localhost:3000)
2. Go to **Dashboards** → **Import**
3. Upload JSON file or paste JSON content
4. Select **Prometheus** as data source
5. Click **Import**

## Customization

- Edit JSON files to add/remove panels
- Adjust alert thresholds in `aibot_alerts.rules`
- Modify scrape interval in `prometheus.yml` (default 15s)

## Grafana Variables

All dashboards support these variables for filtering:
- `$trading_mode` — scalping, day_trading, swing
- `$account` — paper, live, all
- `$symbol` — EURUSD, GBPUSD, XAUUSD, etc.

Set in **Dashboard Settings** → **Variables**.
