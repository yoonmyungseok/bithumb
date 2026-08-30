@echo off
chcp 65001 >nul
cd /d "%~dp0"
title All Quant Bots and Dashboard Background Launcher

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Missing venv\Scripts\python.exe
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
if not "%EXIT_CODE%"=="0" echo [NOT STARTED] Read the message above before closing this window.
pause
exit /b %EXIT_CODE%
