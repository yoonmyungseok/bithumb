@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Sync Remote Data from Linux Server

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

echo ======================================================
echo    [리눅스 서버 ➡️ 로컬 PC] 매매 데이터 다운로드 동기화
echo ======================================================
%PYTHON_BIN% src\sync_manager.py download_data
echo.
pause
