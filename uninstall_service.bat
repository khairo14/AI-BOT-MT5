@echo off
REM ============================================================
REM NSSM Service Uninstallation Script - Task #9
REM Removes AI-BOT-MT5 service from Windows
REM ============================================================

echo.
echo ========================================
echo   AI-BOT-MT5 Service Uninstallation
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

REM Check if service exists
nssm status AIBotBackend >nul 2>&1
if %errorLevel% neq 0 (
    echo [WARNING] Service 'AIBotBackend' not found
    echo Nothing to uninstall.
    pause
    exit /b 0
)

echo Service 'AIBotBackend' found.
echo.
choice /C YN /M "Are you sure you want to remove the service"
if errorlevel 2 goto :cancelled

echo.
echo Stopping service...
nssm stop AIBotBackend

echo Removing service...
nssm remove AIBotBackend confirm

if %errorLevel% neq 0 (
    echo [ERROR] Failed to remove service
    pause
    exit /b 1
)

echo.
echo [OK] Service removed successfully!
echo.
echo The API will no longer start automatically on Windows boot.
echo You can still start it manually using start.bat
echo.
pause
exit /b 0

:cancelled
echo.
echo Uninstallation cancelled.
pause
exit /b 0
