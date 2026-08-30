@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Upbit AI Pro Quant Trading Bot (Restart)

echo ======================================================
echo    Restarting Upbit AI Pro Quant Trading Bot...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

REM 1. Stop previous upbit bot instance
echo [1/2] Stopping existing Upbit bot processes...
venv\Scripts\python.exe src\process_manager.py upbit stop
ping 127.0.0.1 -n 2 > nul

REM 2. Start upbit watchdog & bot
echo [2/2] Starting Upbit Watchdog and Bot Engine...
venv\Scripts\python.exe src\watchdog_upbit.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Upbit Watchdog exited with code %ERRORLEVEL%.
    pause
)
