@echo off
chcp 65001 > nul
cd /d "%~dp0"
title All Quant Bots and Dashboard Launcher

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

%PYTHON_BIN% src\process_manager.py all start
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo  [안내] 백그라운드 무창 모드로 실행 중입니다.
echo  대시보드 접속: http://localhost:7979
echo  이 창은 지금 바로 닫으셔도 프로세스가 유지됩니다.
echo ======================================================
ping 127.0.0.1 -n 4 > nul
exit /b %EXIT_CODE%
