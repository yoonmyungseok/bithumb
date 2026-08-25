@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Upbit AI Pro Quant Trading Bot

echo ======================================================
echo    Starting Upbit AI Pro Quant Trading Bot...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

REM Stop previous upbit bot instance if running
venv\Scripts\python.exe src\process_manager.py upbit stop > nul 2>&1
timeout /t 1 > nul

venv\Scripts\python.exe src\watchdog_upbit.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Upbit Watchdog exited with code %ERRORLEVEL%.
    pause
)
