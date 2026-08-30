@echo off
chcp 65001 > nul
cd /d "%~dp0"
title All Quant Bots and Dashboard Launcher

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

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
