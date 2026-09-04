@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Unified Quant Trading Dashboard Server

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

echo ======================================================
echo  [Dashboard] 기존 대시보드 서버 종료 및 재시작 준비 중...
echo ======================================================
%PYTHON_BIN% src\process_manager.py dashboard stop
%PYTHON_BIN% src\process_manager.py dashboard record_console
ping 127.0.0.1 -n 2 > nul

echo.
echo ======================================================
echo  [Dashboard] 통합 대시보드 게이트웨이 서버 실행 시작
echo  (창을 닫으면 프로세스가 즉시 종료됩니다)
echo  대시보드 접속 주소: http://localhost:7979
echo ======================================================
%PYTHON_BIN% src\dashboard_server.py
echo.
echo [알림] 대시보드 서버가 종료되었습니다.
pause
