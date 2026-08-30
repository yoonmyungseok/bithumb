@echo off
chcp 65001 > nul
cd /d "%~dp0"
title All Quant Bots and Dashboard Status

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

venv\Scripts\python.exe src\process_manager.py all status
pause
