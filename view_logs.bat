@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Bithumb Bot Live Logs
venv\Scripts\python.exe src\process_manager.py logs
pause
