@echo off
title AI-BOT-MT5 Stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Stop script failed -- see messages above.
    pause
)
