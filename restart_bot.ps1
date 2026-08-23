$host.UI.RawUI.WindowTitle = "Restart Bithumb Bot"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " [Restarting Bithumb Auto Trading Bot] " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Stop running processes
$procs = Get-CimInstance Win32_Process | Where-Object { 
    ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and 
    ($_.CommandLine -like "*src\main.py*" -or $_.CommandLine -like "*src/main.py*")
}

if ($procs) {
    Write-Host "🛑 [1/2] Stopping existing bot process(es)..." -ForegroundColor Yellow
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "   - Terminated PID: $($p.ProcessId)" -ForegroundColor Gray
    }
    Start-Sleep -Seconds 1
} else {
    Write-Host "ℹ️ No running bot processes found. Starting freshly..." -ForegroundColor Gray
}

Write-Host "🚀 [2/2] Launching updated bot..." -ForegroundColor Green
Write-Host ""

# 2. Call launcher
& "$scriptDir\launcher.ps1"
