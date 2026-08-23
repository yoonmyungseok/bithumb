@echo off
cd /d "%~dp0"
title 빗썸 자동매매 봇 종료기

echo 🛑 빗썸 자동매매 봇 프로세스를 종료합니다...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' }; if ($procs) { foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force; Write-Host ('[종료 완료] PID: ' + $p.ProcessId) -ForegroundColor Green } } else { Write-Host 'ℹ️ 실행 중인 봇이 없습니다.' -ForegroundColor Yellow }"

echo.
echo ======================================================
echo  ✅ 봇 프로세스가 안전하게 종료되었습니다.
echo ======================================================
timeout /t 3 > nul
