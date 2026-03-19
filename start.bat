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

:: -- export vars for PowerShell to use via $env: --
set EVOTRADE_PY=%VENV_PY%
set EVOTRADE_ROOT=%ROOT%
set EVOTRADE_HOST=%API_HOST%
set EVOTRADE_PORT=%API_PORT%
set EVOTRADE_LOGDIR=%LOG_DIR%
set EVOTRADE_DASH=%DASHBOARD%

:: -- start FastAPI hidden (no popup window) --
echo   Starting FastAPI backend on http://%API_HOST%:%API_PORT% ...
powershell -NoProfile -Command "$a=@('-m','uvicorn','api.main:app','--host',$env:EVOTRADE_HOST,'--port',$env:EVOTRADE_PORT,'--log-level','info'); $p=Start-Process -FilePath $env:EVOTRADE_PY -ArgumentList $a -WorkingDirectory $env:EVOTRADE_ROOT -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText($env:EVOTRADE_LOGDIR+'\api.pid',[string]$p.Id)"
echo   [OK] API started

:: -- health check loop (up to 30 s) --
echo   Waiting for API to become ready...
set /a _tries=0
:health_loop
    timeout /t 1 /nobreak >nul
    curl -s --max-time 2 -o nul "http://%API_HOST%:%API_PORT%/health" >nul 2>&1
    if not errorlevel 1 goto :health_ok
    set /a _tries+=1
    if !_tries! geq 30 (
        echo   [WARN] API did not respond after 30 s.
        echo         Check %LOG_DIR%\api.log for errors.
        goto :launch_dash
    )
    echo     [!_tries!/30] waiting...
goto :health_loop

:health_ok
echo   [OK] API is ready

:launch_dash
:: -- start Next.js dashboard hidden (no popup window) --
echo   Starting Next.js dashboard...
powershell -NoProfile -Command "$a=@('/c','npm run start'); $p=Start-Process -FilePath 'cmd.exe' -ArgumentList $a -WorkingDirectory $env:EVOTRADE_DASH -WindowStyle Hidden -PassThru; [IO.File]::WriteAllText($env:EVOTRADE_LOGDIR+'\dash.pid',[string]$p.Id)"
echo   [OK] Dashboard started

:: -- wait then open browser --
timeout /t 4 /nobreak >nul
start http://localhost:3000

echo.
echo ================================================
echo   EVOTRADE-AI is running!
echo.
echo   API:       http://%API_HOST%:%API_PORT%
echo   Dashboard: http://localhost:3000
echo   Logs:      %LOG_DIR%\api.log
echo.
echo   Run stop.bat to stop the bot.
echo   This window can be closed safely.
echo ================================================
echo.
pause
goto :eof

:fail
echo.
pause
exit /b 1
