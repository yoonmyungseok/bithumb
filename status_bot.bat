@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo 🔍 빗썸 자동매매 봇 실행 상태 확인 중...
echo.

powershell -Command "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' }; if ($procs) { foreach ($p in $procs) { $procObj = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue; $memMB = [math]::Round($procObj.WorkingSet64 / 1MB, 2); Write-Host ('🟢 [실행 중] PID: ' + $p.ProcessId + ' | 메모리 점유: ' + $memMB + ' MB') } } else { Write-Host '⚪ [중지됨] 현재 실행 중인 봇 프로세스가 없습니다.' -ForegroundColor Yellow }"

echo.
pause
