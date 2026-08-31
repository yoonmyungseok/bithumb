@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Unified Dashboard Server (Restart)

echo ======================================================
echo    Restarting Unified Dashboard Server (Port: 7979)...
echo ======================================================

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

REM 1. Stop previous dashboard instance
echo [1/2] Stopping existing Dashboard server...
%PYTHON_BIN% src\process_manager.py dashboard stop
ping 127.0.0.1 -n 2 > nul

REM 2. Start dashboard server
echo [2/2] Starting Unified Dashboard Server in headless mode...
%PYTHON_BIN% src\process_manager.py dashboard start
pause
