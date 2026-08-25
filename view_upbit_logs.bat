@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Upbit Bot Live Logs
venv\Scripts\python.exe src\process_manager.py upbit logs
pause
