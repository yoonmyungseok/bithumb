@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Quant Bots and Dashboard Status Monitor

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

%PYTHON_BIN% src\process_manager.py all status
echo.
pause
