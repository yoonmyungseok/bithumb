@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0launcher.ps1"
if %errorlevel% neq 0 (
    echo.
    echo [종료됨] 오류가 발생하여 멈췄습니다. 창을 닫으려면 아무 키나 누르세요.
    pause > nul
)
