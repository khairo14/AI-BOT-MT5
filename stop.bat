@echo off
title EVOTRADE-AI Stop
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "LOG_DIR=%ROOT%\logs"

echo.
echo ================================================
echo   EVOTRADE-AI  ^|  Stopping...
echo ================================================
echo.

:: -- read saved PIDs --
set API_PID=
set DASH_PID=
if exist "%LOG_DIR%\api.pid"  set /p API_PID=<"%LOG_DIR%\api.pid"
if exist "%LOG_DIR%\dash.pid" set /p DASH_PID=<"%LOG_DIR%\dash.pid"

:: -- stop FastAPI by PID tree --
echo   Stopping FastAPI backend...
if defined API_PID (
    taskkill /PID %API_PID% /T /F >nul 2>&1
    echo   [OK] FastAPI stopped (PID %API_PID%)
) else (
    echo   [WARN] No api.pid file found - scanning by process name...
    powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*uvicorn*api.main*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  [OK] Killed uvicorn PID' $_.ProcessId }"
)

:: -- stop dashboard by PID tree --
echo   Stopping Next.js dashboard...
if defined DASH_PID (
    taskkill /PID %DASH_PID% /T /F >nul 2>&1
    echo   [OK] Dashboard stopped (PID %DASH_PID%)
) else (
    echo   [WARN] No dash.pid file found - scanning by process name...
    powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'node.exe' -and $_.CommandLine -like '*next*start*'} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host '  [OK] Killed node PID' $_.ProcessId }"
)

:: -- clean up PID files --
if exist "%LOG_DIR%\api.pid"   del "%LOG_DIR%\api.pid"   >nul 2>&1
if exist "%LOG_DIR%\dash.pid"  del "%LOG_DIR%\dash.pid"  >nul 2>&1
if exist "%LOG_DIR%\pids.json" del "%LOG_DIR%\pids.json" >nul 2>&1

:: -- stop PostgreSQL Docker container --
echo   Stopping PostgreSQL database...
docker-compose down >nul 2>&1
echo   [OK] PostgreSQL stopped

echo.
echo ================================================
echo   Bot stopped.
echo   Logs are at: %ROOT%\logs\
echo ================================================
echo.
pause
