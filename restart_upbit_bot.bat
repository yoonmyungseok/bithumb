@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Restart Upbit Bot

echo ======================================================
echo  [Restarting Upbit Auto Trading Bot]
echo ======================================================
echo.

venv\Scripts\python.exe src\process_manager.py upbit stop
timeout /t 1 > nul

echo.
call "%~dp0start_upbit_bot.bat"
