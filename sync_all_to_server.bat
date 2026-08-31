@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Deploy Full Project to Linux Server

set "PYTHON_BIN=venv\Scripts\python.exe"
if not exist "%PYTHON_BIN%" (
    set "PYTHON_BIN=python"
)

echo ======================================================
echo    [로컬 PC ➡️ 리눅스 서버] 전체 프로젝트 배포 (코드+설정+데이터)
echo ======================================================
%PYTHON_BIN% src\sync_manager.py upload_all
echo.
pause
