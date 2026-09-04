@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Upbit AI Pro Quant Trading Bot

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

echo ======================================================
echo  [Upbit] 기존 봇 프로세스 종료 및 재시작 준비 중...
echo ======================================================
%PYTHON_BIN% src\process_manager.py upbit stop
%PYTHON_BIN% src\process_manager.py upbit record_console
ping 127.0.0.1 -n 2 > nul

echo.
echo ======================================================
echo  [Upbit] AI Pro Quant Trading Bot 실행 시작
echo  (창을 닫으면 프로세스가 즉시 종료됩니다)
echo ======================================================
%PYTHON_BIN% src\main_upbit.py
echo.
echo [알림] 봇 프로세스가 종료되었습니다.
pause
