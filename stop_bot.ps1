[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$host.UI.RawUI.WindowTitle = "Stop Bithumb Bot"
$procs = Get-CimInstance Win32_Process | Where-Object { 
    ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and 
    ($_.CommandLine -like "*src\main.py*" -or $_.CommandLine -like "*src/main.py*")
}

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " [Stopping Bithumb Auto Trading Bot] " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

if ($procs) {
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force
        Write-Host "🛑 [Terminated] PID: $($p.ProcessId)" -ForegroundColor Green
    }
    Write-Host ""
    Write-Host "✅ All bot processes safely stopped." -ForegroundColor Green
} else {
    Write-Host "ℹ️ No running bot processes found." -ForegroundColor Yellow
}

Start-Sleep -Seconds 2