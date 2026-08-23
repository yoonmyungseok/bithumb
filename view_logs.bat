@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 빗썸 봇 실시간 로그 모니터링

echo ======================================================
echo  📜 빗썸 봇 실시간 로그 모니터링 (창을 닫아도 봇은 계속 돌아갑니다)
echo ======================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$host.UI.RawUI.WindowTitle = '빗썸 봇 실시간 로그'; while (!(Test-Path 'logs\trading.log')) { Write-Host '로그 파일 생성 대기 중...'; Start-Sleep -Seconds 1 }; Get-Content -Path 'logs\trading.log' -Wait -Tail 50 -Encoding UTF8"
pause
