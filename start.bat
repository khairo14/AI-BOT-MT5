@echo off
title AI-BOT-MT5
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Startup failed -- see messages above.
    pause
)
