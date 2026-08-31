@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Sync Local Data to Linux Server

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

echo ======================================================
echo    [로컬 PC ➡️ 리눅스 서버] 매매 데이터 동기화
echo ======================================================
%PYTHON_BIN% src\sync_manager.py upload_data
echo.
pause
