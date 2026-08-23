# ===================================================================
# Bithumb Bot Launcher (PowerShell 5.1 & 7 compatible)
# ===================================================================
$host.UI.RawUI.WindowTitle = "Bithumb Auto Trading Bot Launcher"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " [Bithumb AI Auto Trading Bot Launcher] " -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check .env
if (-not (Test-Path "$scriptDir\.env")) {
    Write-Host "[Error] .env file not found!" -ForegroundColor Red
    Write-Host "Please copy your .env file into this folder." -ForegroundColor Yellow
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

# 2. Check Python & venv
$venvPy = "$scriptDir\venv\Scripts\python.exe"
$venvPyw = "$scriptDir\venv\Scripts\pythonw.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "[1/3] Setting up Python virtual environment (venv)..." -ForegroundColor Yellow
    
    $sysPython = $null
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $sysPython = "python"
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        $sysPython = "py"
    } elseif (Test-Path "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe") {
        $sysPython = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
    } elseif (Test-Path "$env:ProgramFiles\Python311\python.exe") {
        $sysPython = "$env:ProgramFiles\Python311\python.exe"
    }

    if (-not $sysPython) {
        Write-Host "[Error] Python is not installed on this PC!" -ForegroundColor Red
        Write-Host "Please install Python 3.10+ from https://www.python.org/downloads/" -ForegroundColor Yellow
        Write-Host "(Make sure to check 'Add Python to PATH' during installation)" -ForegroundColor Yellow
        Write-Host ""
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Host "Found Python: $sysPython" -ForegroundColor Gray
    & $sysPython -m venv "$scriptDir\venv"

    if (-not (Test-Path $venvPy)) {
        Write-Host "[Error] Failed to create venv." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }

    Write-Host "[2/3] Installing required packages from requirements.txt..." -ForegroundColor Yellow
    & "$scriptDir\venv\Scripts\pip.exe" install -r "$scriptDir\requirements.txt"
    Write-Host "Package installation complete!" -ForegroundColor Green
    Write-Host ""
}

# 3. Check logs directory
if (-not (Test-Path "$scriptDir\logs")) {
    New-Item -ItemType Directory -Path "$scriptDir\logs" | Out-Null
}

# 4. Check if already running
$runningProcs = Get-CimInstance Win32_Process | Where-Object { 
    $_.CommandLine -like "*src\main.py*" -or $_.CommandLine -like "*src/main.py*" 
}

if ($runningProcs) {
    Write-Host "Bot is already running in the background." -ForegroundColor Cyan
} else {
    Write-Host "[3/3] Starting bot process in background..." -ForegroundColor Gray
    Start-Process -FilePath $venvPyw -ArgumentList "$scriptDir\src\main.py" -WorkingDirectory $scriptDir
    Start-Sleep -Seconds 3
}

# 5. Verify status
$runningProcs = Get-CimInstance Win32_Process | Where-Object { 
    $_.CommandLine -like "*src\main.py*" -or $_.CommandLine -like "*src/main.py*" 
}

if ($runningProcs) {
    foreach ($p in $runningProcs) {
        $procObj = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
        $memMB = [math]::Round($procObj.WorkingSet64 / 1MB, 2)
        Write-Host "[Active] PID: $($p.ProcessId) | Memory: $memMB MB" -ForegroundColor Green
    }
} else {
    Write-Host "[Error] Failed to start bot. Running directly to display error traceback:" -ForegroundColor Red
    Write-Host "------------------------------------------------------" -ForegroundColor DarkGray
    & $venvPy "$scriptDir\src\main.py"
    Write-Host "------------------------------------------------------" -ForegroundColor DarkGray
    Read-Host "Press Enter to exit"
    exit 1
}

# 6. Stream logs
Write-Host ""
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host " [Live Log Streaming] (Close window anytime - bot keeps running!)" -ForegroundColor Cyan
Write-Host "======================================================" -ForegroundColor Cyan
Write-Host ""

$logFile = "$scriptDir\logs\trading.log"
while (-not (Test-Path $logFile)) {
    Write-Host "Waiting for log file creation..." -ForegroundColor Gray
    Start-Sleep -Seconds 1
}

Get-Content -Path $logFile -Wait -Tail 30 -Encoding UTF8