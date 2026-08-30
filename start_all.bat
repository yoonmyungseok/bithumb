@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Start All Bots and Unified Dashboard

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Missing venv\Scripts\python.exe
    pause
    exit /b 1
)

venv\Scripts\python.exe src\process_manager.py all start
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
