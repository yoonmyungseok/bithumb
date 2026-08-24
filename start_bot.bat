@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Launcher failed with code %ERRORLEVEL%.
    pause
)
