@echo off
REM ============================================================
REM NSSM Service Installation Script - Task #9
REM Installs AI-BOT-MT5 API as a Windows service for automatic restart
REM ============================================================

echo.
echo ========================================
echo   AI-BOT-MT5 Service Installation
echo   Task #9: Automated Restart (NSSM)
echo ========================================
echo.

REM Check if running as administrator
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERROR] This script requires administrator privileges.
    echo Please right-click and select "Run as Administrator"
    pause
    exit /b 1
)

REM Check if NSSM is installed
where nssm >nul 2>&1
if %errorLevel% neq 0 (
    echo [WARNING] NSSM not found!
    echo.
    echo Installing NSSM via Chocolatey...
    echo.
    
    REM Check if Chocolatey is installed
    where choco >nul 2>&1
    if %errorLevel% neq 0 (
        echo [ERROR] Chocolatey not installed!
        echo.
        echo Please install Chocolatey first:
        echo https://chocolatey.org/install
        echo.
        echo Or download NSSM manually:
        echo https://nssm.cc/download
        pause
        exit /b 1
    )
    
    choco install nssm -y
    if %errorLevel% neq 0 (
        echo [ERROR] Failed to install NSSM
        pause
        exit /b 1
    )
)

echo [OK] NSSM is installed
echo.

REM Get project directory
set PROJECT_DIR=%~dp0
set PROJECT_DIR=%PROJECT_DIR:~0,-1%

echo Project directory: %PROJECT_DIR%
echo.

REM Check if service already exists
nssm status AIBotBackend >nul 2>&1
if %errorLevel% == 0 (
    echo [WARNING] Service 'AIBotBackend' already exists!
    echo.
    choice /C YN /M "Do you want to remove and recreate it"
    if errorlevel 2 goto :skip_install
    
    echo Stopping existing service...
    nssm stop AIBotBackend
    
    echo Removing existing service...
    nssm remove AIBotBackend confirm
)

:skip_install

REM Install the service
echo.
echo Installing AIBotBackend service...
echo.

nssm install AIBotBackend "%PROJECT_DIR%\.venv\Scripts\python.exe"
if %errorLevel% neq 0 (
    echo [ERROR] Failed to install service
    pause
    exit /b 1
)

REM Configure service parameters
echo Configuring service parameters...

nssm set AIBotBackend AppDirectory "%PROJECT_DIR%"
nssm set AIBotBackend AppParameters "api/main.py"

REM Logging
nssm set AIBotBackend AppStdout "%PROJECT_DIR%\logs\service.log"
nssm set AIBotBackend AppStderr "%PROJECT_DIR%\logs\service_error.log"
nssm set AIBotBackend AppStdoutCreationDisposition 4
nssm set AIBotBackend AppStderrCreationDisposition 4

REM Restart behavior
nssm set AIBotBackend AppExit Default Restart
nssm set AIBotBackend AppRestartDelay 5000

REM Throttle restart attempts (prevent infinite restart loop)
nssm set AIBotBackend AppThrottle 15000

REM Startup type (delayed auto-start to allow MT5 terminal to start first)
nssm set AIBotBackend Start SERVICE_DELAYED_AUTO_START

REM Display name and description
nssm set AIBotBackend DisplayName "AI-BOT-MT5 Backend API"
nssm set AIBotBackend Description "Trading bot backend service - XM MT5 via Python (auto-restart enabled)"

echo [OK] Service configured successfully
echo.

REM Start the service
echo Starting service...
nssm start AIBotBackend

if %errorLevel% neq 0 (
    echo [WARNING] Service started with warnings
    echo Check logs: %PROJECT_DIR%\logs\service_error.log
) else (
    echo [OK] Service started successfully
)

echo.
echo ========================================
echo   Installation Complete!
echo ========================================
echo.
echo Service Name: AIBotBackend
echo Display Name: AI-BOT-MT5 Backend API
echo Status: Running
echo.
echo Useful Commands:
echo   nssm status AIBotBackend    - Check service status
echo   nssm start AIBotBackend     - Start service
echo   nssm stop AIBotBackend      - Stop service
echo   nssm restart AIBotBackend   - Restart service
echo   nssm remove AIBotBackend    - Uninstall service
echo.
echo Logs:
echo   Output: %PROJECT_DIR%\logs\service.log
echo   Errors: %PROJECT_DIR%\logs\service_error.log
echo.
echo The service will start automatically on Windows boot.
echo.
pause
