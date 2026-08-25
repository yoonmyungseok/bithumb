@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Bithumb Bot Background Launcher

echo ======================================================
echo   [빗썸 AI 퀀트 트레이딩 봇 백그라운드 가동 시작]
echo ======================================================

if not exist "venv\Scripts\pythonw.exe" (
    echo [ERROR] 가상환경 'venv' 또는 'pythonw.exe'를 찾을 수 없습니다!
    pause
    exit /b 1
)

REM 기존 빗썸 봇 프로세스 정리
venv\Scripts\python.exe src\process_manager.py bithumb stop > nul 2>&1
timeout /t 1 > nul

REM 콘솔 창 없이 백그라운드에서 완전 무창 실행 (pythonw.exe)
start "" venv\Scripts\pythonw.exe src\watchdog.py

timeout /t 1 > nul

echo.
echo 🟢 [가동 완료] 빗썸 봇이 백그라운드에서 24시간 무중단 가동 중입니다.
echo.
echo • 웹 대시보드: http://localhost:7979
echo • 상태 확인: status_bot.bat
echo • 실시간 로그: view_logs.bat
echo • 봇 종료: stop_bot.bat
echo ======================================================
echo (이 안내 창은 3초 후 자동으로 닫힙니다.)
timeout /t 3 > nul
