@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Unified Dashboard Server (Restart)

echo ======================================================
echo    Restarting Unified Dashboard Server (Port: 7979)...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

REM 1. Stop previous dashboard instance
echo [1/2] Stopping existing Dashboard server...
venv\Scripts\python.exe src\process_manager.py dashboard stop
ping 127.0.0.1 -n 2 > nul

REM 2. Start dashboard server
echo [2/2] Starting Unified Dashboard Server on http://localhost:7979...
venv\Scripts\python.exe src\dashboard_server.py
pause
