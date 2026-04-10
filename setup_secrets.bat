@echo off
REM ============================================================
REM Setup Script for AI-BOT-MT5
REM Creates .env file and guides user through initial setup
REM ============================================================

echo.
echo ========================================
echo   AI-BOT-MT5 Initial Setup
echo ========================================
echo.

REM Check if .env already exists
if exist .env (
    echo [WARNING] .env file already exists!
    echo.
    choice /C YN /M "Do you want to overwrite it"
    if errorlevel 2 goto :skip_copy
)

REM Copy template
echo Copying .env.example to .env...
copy .env.example .env >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy .env.example
    pause
    exit /b 1
)
echo [OK] .env file created successfully
echo.

:skip_copy

REM Prompt for MT5 credentials
echo ========================================
echo   MT5 Demo Account Setup
echo ========================================
echo.
set /p DEMO_LOGIN="Enter MT5 Demo Login: "
set /p DEMO_PASSWORD="Enter MT5 Demo Password: "
set /p DEMO_SERVER="Enter MT5 Demo Server (e.g., XMGlobal-MT5 6): "

echo.
echo ========================================
echo   MT5 Live Account Setup
echo ========================================
echo.
set /p LIVE_LOGIN="Enter MT5 Live Login: "
set /p LIVE_PASSWORD="Enter MT5 Live Password: "
set /p LIVE_SERVER="Enter MT5 Live Server (e.g., XMGlobal-MT5 18): "

echo.
echo ========================================
echo   API Configuration
echo ========================================
echo.
echo Generating API secret key...

REM Generate random API key using PowerShell
for /f "delims=" %%i in ('powershell -Command "Add-Type -AssemblyName System.Web; [System.Web.Security.Membership]::GeneratePassword(32, 5)"') do set API_KEY=%%i

echo [OK] API secret key generated: %API_KEY%
echo.

REM Update .env file
echo Updating .env file with your credentials...

REM Use PowerShell to safely update .env file
powershell -Command ^
    "$content = Get-Content .env; " ^
    "$content = $content -replace '^MT5_DEMO_LOGIN=.*', 'MT5_DEMO_LOGIN=%DEMO_LOGIN%'; " ^
    "$content = $content -replace '^MT5_DEMO_PASSWORD=.*', 'MT5_DEMO_PASSWORD=%DEMO_PASSWORD%'; " ^
    "$content = $content -replace '^MT5_DEMO_SERVER=.*', 'MT5_DEMO_SERVER=%DEMO_SERVER%'; " ^
    "$content = $content -replace '^MT5_LIVE_LOGIN=.*', 'MT5_LIVE_LOGIN=%LIVE_LOGIN%'; " ^
    "$content = $content -replace '^MT5_LIVE_PASSWORD=.*', 'MT5_LIVE_PASSWORD=%LIVE_PASSWORD%'; " ^
    "$content = $content -replace '^MT5_LIVE_SERVER=.*', 'MT5_LIVE_SERVER=%LIVE_SERVER%'; " ^
    "$content = $content -replace '^API_SECRET_KEY=.*', 'API_SECRET_KEY=%API_KEY%'; " ^
    "$content | Set-Content .env"

if errorlevel 1 (
    echo [ERROR] Failed to update .env file
    echo Please manually edit .env and fill in your credentials
    pause
    exit /b 1
)

echo [OK] .env file updated successfully
echo.

REM Validate setup
echo ========================================
echo   Validating Configuration
echo ========================================
echo.

python engine\validate_secrets.py
if errorlevel 1 (
    echo [ERROR] Validation failed!
    echo Please review the errors above and fix your .env file
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Setup Complete!
echo ========================================
echo.
echo Your credentials are stored securely in .env
echo This file is excluded from git (.gitignore)
echo.
echo Next steps:
echo   1. Start the backend:  start.bat
echo   2. Open dashboard:     http://localhost:3000
echo.
pause
