@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo 🛑 빗썸 자동매매 봇 백그라운드 프로세스를 종료합니다...

powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('[종료됨] PID: ' + $_.ProcessId) }"

echo.
echo ======================================================
echo  ✅ 봇 프로세스가 안전하게 종료되었습니다.
echo ======================================================
timeout /t 3 > nul
