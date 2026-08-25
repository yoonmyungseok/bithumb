@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Upbit Bot Background Launcher

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Missing venv\Scripts\python.exe
    pause
    exit /b 1
)

REM Keep this file ASCII-only: cmd.exe parses batch text before code-page changes.
venv\Scripts\python.exe src\process_manager.py upbit start
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo ======================================================
echo Dashboard: http://localhost:7980
echo Status: status_upbit_bot.bat
echo Logs: view_upbit_logs.bat
echo Stop: stop_upbit_bot.bat
echo ======================================================
if not "%EXIT_CODE%"=="0" echo [NOT STARTED] Read the message above before closing this window.
pause
exit /b %EXIT_CODE%
