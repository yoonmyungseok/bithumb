@echo off
cd /d "%~dp0"
title Gemini API 모델 목록 조회기

if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" "%~dp0list_models.py"
) else (
    python "%~dp0list_models.py"
)

echo.
pause
