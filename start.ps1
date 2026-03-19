#Requires -Version 5.1
<#
.SYNOPSIS
    Start the AI-BOT-MT5 trading system (FastAPI backend + Next.js dashboard).

.DESCRIPTION
    1. Checks prerequisites (.env exists, .venv exists, node_modules exist)
    2. Verifies MT5 terminal is running (warns if not — required for trade execution)
    3. Starts the FastAPI/uvicorn backend in a minimised window
    4. Waits for the API health check to pass (up to 30 s)
    5. Starts the Next.js dashboard (next start — uses the pre-built .next directory)
    6. Opens http://localhost:3000 in the default browser

.NOTES
    Run from the repo root:   .\start.ps1
    Stop everything:          .\stop.ps1

    First-time setup:
        python -m venv .venv
        .venv\Scripts\pip install -r requirements.txt
        .venv\Scripts\pip install -r requirements-ml.txt
        cd dashboard ; npm install ; npm run build ; cd ..
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── paths ────────────────────────────────────────────────────────────────────
$Root      = $PSScriptRoot
$EnvFile   = Join-Path $Root ".env"
$Venv      = Join-Path $Root ".venv"
$VenvPy    = Join-Path $Venv "Scripts\python.exe"
$Dashboard = Join-Path $Root "dashboard"
$LogDir    = Join-Path $Root "logs"
$PidFile   = Join-Path $Root "logs\pids.json"

# ── helpers ──────────────────────────────────────────────────────────────────
function Write-Step([string]$msg) {
    Write-Host "  $msg" -ForegroundColor Cyan
}
function Write-Ok([string]$msg) {
    Write-Host "  [OK] $msg" -ForegroundColor Green
}
function Write-Warn([string]$msg) {
    Write-Host "  [WARN] $msg" -ForegroundColor Yellow
}
function Write-Fail([string]$msg) {
    Write-Host "  [FAIL] $msg" -ForegroundColor Red
    exit 1
}
function Get-EnvValue([string]$Path, [string]$Key, [string]$Default) {
    $raw   = Get-Content $Path -Raw
    $match = [regex]::Match($raw, "(?m)^$Key\s*=\s*(.+)$")
    if ($match.Success) { return $match.Groups[1].Value.Trim() }
    return $Default
}

# ── banner ───────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host "  AI-BOT-MT5  |  XM Trading System" -ForegroundColor White
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host ""

# ── prerequisite checks ───────────────────────────────────────────────────────
Write-Step "Checking prerequisites..."

if (-not (Test-Path $EnvFile)) {
    Write-Fail ".env file not found. Copy .env.example to .env and fill in your MT5 credentials."
}
Write-Ok ".env found"

if (-not (Test-Path $VenvPy)) {
    Write-Fail "Python venv not found at .venv\. Run:  python -m venv .venv  then install requirements."
}
Write-Ok "Python venv found"

$NextBuild = Join-Path $Dashboard ".next\BUILD_ID"
if (-not (Test-Path $NextBuild)) {
    Write-Warn "Dashboard not built. Running 'npm run build' now (this may take ~30 s)..."
    Push-Location $Dashboard
    try {
        npm run build | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Fail "Dashboard build failed. Fix TypeScript errors and retry." }
    } finally {
        Pop-Location
    }
    Write-Ok "Dashboard built"
} else {
    Write-Ok "Dashboard build found"
}

# ── MT5 terminal check ────────────────────────────────────────────────────────
Write-Step "Checking MT5 terminal..."
$mt5Proc = Get-Process -Name "terminal64" -ErrorAction SilentlyContinue
if (-not $mt5Proc) {
    Write-Warn "MetaTrader 5 terminal (terminal64.exe) is NOT running."
    Write-Warn "Trade execution requires MT5 to be open. Starting bot in read-only mode."
} else {
    Write-Ok "MT5 terminal running (PID $($mt5Proc.Id))"
}

# ── create log dir ────────────────────────────────────────────────────────────
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ── load .env to read API port ────────────────────────────────────────────────
$ApiHost = Get-EnvValue $EnvFile 'API_HOST' '127.0.0.1'
$ApiPort = Get-EnvValue $EnvFile 'API_PORT' '8000'
$ApiUrl  = "http://$($ApiHost):$($ApiPort)"

# ── start FastAPI backend ─────────────────────────────────────────────────────
Write-Step "Starting FastAPI backend on $ApiUrl ..."

$apiLogOut = Join-Path $LogDir "api_out.log"
$apiLogErr = Join-Path $LogDir "api_err.log"

$apiProc = Start-Process `
    -FilePath $VenvPy `
    -ArgumentList "-m", "uvicorn", "api.main:app",
                  "--host", $ApiHost,
                  "--port", $ApiPort,
                  "--log-level", "info" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $apiLogOut `
    -RedirectStandardError  $apiLogErr `
    -WindowStyle Hidden `
    -PassThru

Write-Ok "API process started (PID $($apiProc.Id))"

# ── wait for API health ────────────────────────────────────────────────────────
Write-Step "Waiting for API to become ready..."
$ready   = $false
$timeout = 30
for ($i = 0; $i -lt $timeout; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri "$ApiUrl/docs" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        if ($resp.StatusCode -lt 400) { $ready = $true; break }
    } catch { }
    Write-Host "    ($($i+1)/$timeout) waiting..." -ForegroundColor DarkGray
}

if (-not $ready) {
    Write-Warn "API did not respond within ${timeout}s."
    Write-Warn "Check logs\api_err.log for startup errors."
    Write-Warn "Continuing to start dashboard anyway..."
} else {
    Write-Ok "API is ready at $ApiUrl"
}

# ── start Next.js dashboard ────────────────────────────────────────────────────
Write-Step "Starting Next.js dashboard on http://localhost:3000 ..."

# Resolve node.exe from PATH
$nodeCmd  = Get-Command "node" -ErrorAction SilentlyContinue
$nodePath = $null
if ($nodeCmd) { $nodePath = $nodeCmd.Source }
if (-not $nodePath) { Write-Fail "node.exe not found in PATH. Install Node.js 18+." }

$dashLogOut = Join-Path $LogDir "dashboard_out.log"
$dashLogErr = Join-Path $LogDir "dashboard_err.log"

$dashProc = Start-Process `
    -FilePath $nodePath `
    -ArgumentList (Join-Path $Dashboard "node_modules\.bin\next"), "start" `
    -WorkingDirectory $Dashboard `
    -RedirectStandardOutput $dashLogOut `
    -RedirectStandardError  $dashLogErr `
    -WindowStyle Hidden `
    -PassThru

Write-Ok "Dashboard process started (PID $($dashProc.Id))"

# ── save PIDs for stop.ps1 ────────────────────────────────────────────────────
$pids = @{
    api       = $apiProc.Id
    dashboard = $dashProc.Id
    started   = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
}
$pids | ConvertTo-Json | Set-Content $PidFile
Write-Ok "PIDs saved to logs\pids.json"

# ── open browser ──────────────────────────────────────────────────────────────
Start-Sleep -Seconds 2
Write-Step "Opening dashboard in browser..."
Start-Process "http://localhost:3000"

# ── done ─────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host "  Bot is running!" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard  -> http://localhost:3000" -ForegroundColor White
Write-Host "  API        -> $ApiUrl" -ForegroundColor White
Write-Host "  API docs   -> $ApiUrl/docs" -ForegroundColor White
Write-Host ""
Write-Host "  Logs       -> $LogDir" -ForegroundColor DarkGray
Write-Host "  Stop bot   -> .\stop.ps1" -ForegroundColor DarkGray
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host ""
Read-Host "Press Enter to close this window"
