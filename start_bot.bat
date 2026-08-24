@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Bithumb AI Pro Quant Trading Bot

echo ======================================================
echo    Starting Bithumb AI Pro Quant Trading Bot...
echo ======================================================

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment 'venv' not found!
    pause
    exit /b 1
)

venv\Scripts\python.exe src\main.py
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Bot exited with code %ERRORLEVEL%.
    pause
)
