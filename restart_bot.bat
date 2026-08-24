@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Restart Bithumb Bot

echo ======================================================
echo  [Restarting Bithumb Auto Trading Bot]
echo ======================================================
echo.

venv\Scripts\python.exe src\process_manager.py stop
timeout /t 1 > nul

echo.
call "%~dp0start_bot.bat"
