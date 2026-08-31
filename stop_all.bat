@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Stop All Quant Bots and Dashboard

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

%PYTHON_BIN% src\process_manager.py all stop
pause
