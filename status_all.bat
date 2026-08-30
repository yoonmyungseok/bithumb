@echo off
chcp 65001 >nul
cd /d "%~dp0"
title All Bots and Dashboard Status

venv\Scripts\python.exe src\process_manager.py all status
pause
