#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the AI-BOT-MT5 trading system.

.DESCRIPTION
    Reads logs\pids.json written by start.ps1 and gracefully terminates
    the FastAPI backend and Next.js dashboard processes.
    Falls back to process-name scanning if the PID file is missing.

.NOTES
    Run from the repo root:   .\stop.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "SilentlyContinue"

$Root    = $PSScriptRoot
$PidFile = Join-Path $Root "logs\pids.json"

function Write-Step([string]$msg) { Write-Host "  $msg" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  [OK] $msg"   -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  [WARN] $msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host "  AI-BOT-MT5  |  Stopping..." -ForegroundColor White
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host ""

function Stop-ById([int]$pid_, [string]$label) {
    $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Step "Stopping $label (PID $pid_)..."
        $proc | Stop-Process -Force -ErrorAction SilentlyContinue
        $proc.WaitForExit(3000) | Out-Null
        Write-Ok "$label stopped"
    } else {
        Write-Warn "$label (PID $pid_) was not running"
    }
}

# ── read saved PIDs ───────────────────────────────────────────────────────────
if (Test-Path $PidFile) {
    try {
        $saved = Get-Content $PidFile -Raw | ConvertFrom-Json
        Stop-ById $saved.api       "FastAPI backend"
        Stop-ById $saved.dashboard "Next.js dashboard"
        Remove-Item $PidFile -Force
        Write-Ok "PID file removed"
    } catch {
        Write-Warn "Could not parse pids.json — falling back to name scan"
        goto fallback
    }
} else {
    Write-Warn "logs\pids.json not found — scanning by process name"
    :fallback

    # Kill any uvicorn workers attached to this repo path
    Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*uvicorn*api.main*"
    } | ForEach-Object {
        Write-Step "Killing python/uvicorn (PID $($_.Id))..."
        Stop-Process -Id $_.Id -Force
        Write-Ok "Stopped PID $($_.Id)"
    }

    # Kill any node process running `next start`
    Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*next*start*"
    } | ForEach-Object {
        Write-Step "Killing node/next (PID $($_.Id))..."
        Stop-Process -Id $_.Id -Force
        Write-Ok "Stopped PID $($_.Id)"
    }
}

Write-Host ""
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host "  Bot stopped." -ForegroundColor Green
Write-Host "  Logs are at: $Root\logs\" -ForegroundColor DarkGray
Write-Host "================================================" -ForegroundColor DarkCyan
Write-Host ""
