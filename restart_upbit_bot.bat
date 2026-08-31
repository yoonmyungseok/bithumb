@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Upbit AI Pro Quant Trading Bot (Restart)

echo ======================================================
echo    Restarting Upbit AI Pro Quant Trading Bot...
echo ======================================================

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

REM 1. Stop previous upbit bot instance
echo [1/2] Stopping existing Upbit bot processes...
%PYTHON_BIN% src\process_manager.py upbit stop
ping 127.0.0.1 -n 2 > nul

REM 2. Start upbit watchdog & bot
echo [2/2] Starting Upbit Watchdog and Bot Engine in headless mode...
%PYTHON_BIN% src\process_manager.py upbit start
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Upbit Watchdog exited with code %ERRORLEVEL%.
    pause
)
