@echo off
setlocal enabledelayedexpansion
title EVOTRADE-AI
cd /d "%~dp0"

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"

set "ENV_FILE=%ROOT%\.env"
set "VENV_PY=%ROOT%\.venv\Scripts\python.exe"
set "DASHBOARD=%ROOT%\dashboard"
set "LOG_DIR=%ROOT%\logs"

echo.
echo ================================================
echo   EVOTRADE-AI  ^|  XM Trading System
echo ================================================
echo.

:: -- prerequisites --
echo   Checking prerequisites...

if not exist "%ENV_FILE%" (
    echo   [FAIL] .env file not found.
    echo         Copy .env.example to .env and fill in your MT5 credentials.
    goto :fail
)
echo   [OK] .env found

if not exist "%VENV_PY%" (
    echo   [FAIL] Python venv not found at .venv\
    echo         Run: python -m venv .venv
    echo         Then: .venv\Scripts\pip install -r requirements.txt
    goto :fail
)
echo   [OK] Python venv found

if not exist "%DASHBOARD%\.next\BUILD_ID" (
    echo   [WARN] Dashboard not built -- running npm run build now...
    pushd "%DASHBOARD%"
    call npm run build
    if errorlevel 1 (
        echo   [FAIL] Dashboard build failed.
        popd
        goto :fail
    )
    popd
    echo   [OK] Dashboard built
) else (
    echo   [OK] Dashboard build found
)

:: -- MT5 check --
tasklist /FI "IMAGENAME eq terminal64.exe" 2>nul | find /I "terminal64.exe" >nul 2>&1
if errorlevel 1 (
    echo   [WARN] MetaTrader 5 ^(terminal64.exe^) is NOT running.
    echo   [WARN] Trade execution requires MT5. Starting in read-only mode.
) else (
    echo   [OK] MT5 terminal running
)

:: -- create log dir --
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

:: -- read API host/port from .env --
set "API_HOST=127.0.0.1"
set "API_PORT=8000"
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if /i "%%A"=="API_HOST" set "API_HOST=%%B"
    if /i "%%A"=="API_PORT" set "API_PORT=%%B"
)

echo   Starting FastAPI backend on http://%API_HOST%:%API_PORT% ...

:: -- start FastAPI (named window so stop.bat can identify it) --
start "EVOTRADE-API" /min "%VENV_PY%" -m uvicorn api.main:app --host %API_HOST% --port %API_PORT% --log-level info

:: -- health check loop (up to 30 s) --
echo   Waiting for API to become ready...
set /a _tries=0
:health_loop
    timeout /t 1 /nobreak >nul
    curl -s --max-time 2 -o nul "http://%API_HOST%:%API_PORT%/health" >nul 2>&1
    if not errorlevel 1 goto :health_ok
    set /a _tries+=1
    if !_tries! geq 30 (
        echo   [WARN] API did not respond after 30 s - it may still be starting.
        echo         Check the EVOTRADE-API window for errors.
        goto :launch_dash
    )
    echo   [!_tries!/30] waiting...
goto :health_loop

:health_ok
echo   [OK] API is ready

:launch_dash
:: -- start Next.js dashboard --
echo   Starting Next.js dashboard...
pushd "%DASHBOARD%"
start "EVOTRADE-DASH" /min cmd /c npm run start
popd

:: -- wait then open browser --
timeout /t 4 /nobreak >nul
start http://localhost:3000

echo.
echo ================================================
echo   EVOTRADE-AI is running!
echo.
echo   API:       http://%API_HOST%:%API_PORT%
echo   Dashboard: http://localhost:3000
echo   Logs:      %LOG_DIR%\
echo.
echo   Run stop.bat to stop the bot.
echo ================================================
echo.
pause
goto :eof

:fail
echo.
pause
exit /b 1
