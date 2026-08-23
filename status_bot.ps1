[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$host.UI.RawUI.WindowTitle = "Bithumb Bot Status"
$procs = Get-CimInstance Win32_Process | Where-Object { 
    ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and 
    ($_.CommandLine -like "*src\main.py*" -or $_.CommandLine -like "*src/main.py*")
}

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " [Bithumb Bot Running Status] " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

if ($procs) {
    $mainProc = $procs | Sort-Object { (Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue).WorkingSet64 } -Descending | Select-Object -First 1
    $procObj = Get-Process -Id $mainProc.ProcessId -ErrorAction SilentlyContinue
    $memMB = [math]::Round($procObj.WorkingSet64 / 1MB, 2)
    Write-Host "🟢 [정상 실행 중] PID: $($mainProc.ProcessId) | 메모리 점유: $memMB MB (초경량 구동)" -ForegroundColor Green
} else {
    Write-Host "⚪ [중지됨] 현재 실행 중인 봇 프로세스가 없습니다." -ForegroundColor Yellow
}

Write-Host ""
Read-Host "Press Enter to close"