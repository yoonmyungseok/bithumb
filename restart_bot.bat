@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Bithumb AI Pro Quant Trading Bot (Restart)

echo ======================================================
echo    Restarting Bithumb AI Pro Quant Trading Bot...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

REM 1. Stop previous bithumb bot instance
echo [1/2] Stopping existing Bithumb bot processes...
venv\Scripts\python.exe src\process_manager.py bithumb stop
ping 127.0.0.1 -n 2 > nul

REM 2. Start watchdog & bot
echo [2/2] Starting Bithumb Watchdog and Bot Engine...
venv\Scripts\python.exe src\watchdog.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Watchdog exited with code %ERRORLEVEL%.
    pause
)
