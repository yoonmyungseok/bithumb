@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Restart All Quant Bots and Dashboard

echo ======================================================
echo    Restarting All Quant Bots and Dashboard Server...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

REM 1. Stop all running instances
echo [1/2] Stopping all running bots and dashboard...
venv\Scripts\python.exe src\process_manager.py all stop
ping 127.0.0.1 -n 3 > nul

REM 2. Start all instances in background
echo [2/2] Starting all bots and dashboard...
venv\Scripts\python.exe src\process_manager.py all start
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ======================================================
echo  Unified Dashboard: http://localhost:7979
echo  Check Status:      status_all.bat
echo  Stop All:          stop_all.bat
echo ======================================================
pause
exit /b %EXIT_CODE%
