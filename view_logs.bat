@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $host.UI.RawUI.WindowTitle = 'Bithumb Bot Live Logs'; while (!(Test-Path 'logs\trading.log')) { Write-Host 'Waiting for log file...' -ForegroundColor Gray; Start-Sleep -Seconds 1 }; Get-Content -Path 'logs\trading.log' -Wait -Tail 50 -Encoding UTF8"
