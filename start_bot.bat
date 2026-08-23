@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 빗썸 자동매매 봇 실행 및 실시간 모니터링

echo ======================================================
echo  🚀 빗썸 API 2.0 퀀트 자동매매 봇 시작 중...
echo ======================================================
echo.

:: 1. 이미 실행 중인지 확인
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' };" ^
    "if ($procs) {" ^
    "    Write-Host 'ℹ️ 이미 봇이 백그라운드에서 실행 중입니다.' -ForegroundColor Cyan;" ^
    "} else {" ^
    "    Write-Host '[1/2] 봇 프로세스 백그라운드 실행 중...' -ForegroundColor Gray;" ^
    "    Start-Process -FilePath '%~dp0venv\Scripts\pythonw.exe' -ArgumentList '%~dp0src\main.py' -WorkingDirectory '%~dp0';" ^
    "    Start-Sleep -Seconds 2;" ^
    "}"

echo.
echo [2/2] 실행 상태 점검:
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' };" ^
    "if ($procs) {" ^
    "    foreach ($p in $procs) {" ^
    "        $procObj = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue;" ^
    "        $memMB = [math]::Round($procObj.WorkingSet64 / 1MB, 2);" ^
    "        Write-Host ('🟢 [정상 실행 중] PID: ' + $p.ProcessId + ' | 메모리 점유: ' + $memMB + ' MB') -ForegroundColor Green;" ^
    "    }" ^
    "} else {" ^
    "    Write-Host '❌ 봇 시작에 실패했습니다. logs\trading.log를 확인하세요.' -ForegroundColor Red;" ^
    "}"

echo.
echo ======================================================
echo  📜 실시간 로그 모니터링 (Ctrl + C 누르면 닫힘)
echo  ※ 이 창을 닫아도 봇은 백그라운드에서 계속 24시간 돌아갑니다!
echo ======================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "while (!(Test-Path 'logs\trading.log')) { Write-Host '로그 생성 대기 중...'; Start-Sleep -Seconds 1 };" ^
    "Get-Content -Path 'logs\trading.log' -Wait -Tail 30 -Encoding UTF8"

pause
