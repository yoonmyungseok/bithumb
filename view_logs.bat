@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo ======================================================
echo  📜 빗썸 봇 실시간 로그 모니터링 (Ctrl + C 누르면 닫힘)
echo ======================================================
echo.

if not exist "logs\trading.log" (
    echo [안내] 아직 로그 파일(logs\trading.log)이 생성되지 않았습니다.
    echo 봇이 실행되면 자동으로 로그가 기록됩니다.
    echo.
)

powershell -NoExit -Command "$host.UI.RawUI.WindowTitle = '빗썸 봇 실시간 로그'; if (Test-Path 'logs\trading.log') { Get-Content -Path 'logs\trading.log' -Wait -Tail 50 } else { Write-Host '로그 파일 대기 중...'; while (!(Test-Path 'logs\trading.log')) { Start-Sleep -Seconds 1 }; Get-Content -Path 'logs\trading.log' -Wait -Tail 50 }"
