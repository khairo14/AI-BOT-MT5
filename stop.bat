@echo off
title EVOTRADE-AI Stop
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

echo.
echo ================================================
echo   EVOTRADE-AI  ^|  Stopping...
echo ================================================
echo.

:: -- stop FastAPI by window title (kills python + worker children) --
echo   Stopping FastAPI backend...
taskkill /FI "WINDOWTITLE eq EVOTRADE-API" /T /F >nul 2>&1
if errorlevel 1 (
    echo   [WARN] FastAPI window not found - may already be stopped
) else (
    echo   [OK] FastAPI stopped
)

:: -- stop dashboard by window title (kills cmd + node children) --
echo   Stopping Next.js dashboard...
taskkill /FI "WINDOWTITLE eq EVOTRADE-DASH" /T /F >nul 2>&1
if errorlevel 1 (
    echo   [WARN] Dashboard window not found - may already be stopped
) else (
    echo   [OK] Dashboard stopped
)

:: -- fallback: kill any remaining uvicorn or next/node processes --
echo   Cleaning up any remaining processes...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*uvicorn*api.main*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  [OK] Killed uvicorn PID' $_.ProcessId }"
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'node.exe' -and $_.CommandLine -like '*next*start*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  [OK] Killed node PID' $_.ProcessId }"

:: -- clean up stale PID files if present --
if exist "%ROOT%\logs\pids.json" del "%ROOT%\logs\pids.json" >nul 2>&1
if exist "%ROOT%\logs\pids.txt"  del "%ROOT%\logs\pids.txt"  >nul 2>&1

echo.
echo ================================================
echo   Bot stopped.
echo   Logs are at: %ROOT%\logs\
echo ================================================
echo.
pause
