@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON_CMD=python"
if exist "%PROJECT_DIR%venv\Scripts\python.exe" set "PYTHON_CMD=%PROJECT_DIR%venv\Scripts\python.exe"

%PYTHON_CMD% -m unittest discover -s "%PROJECT_DIR%tests" -v
exit /b %ERRORLEVEL%
