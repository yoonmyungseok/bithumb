@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 빗썸 자동매매 봇 실행기

echo ======================================================
echo  🚀 빗썸 API 2.0 퀀트 자동매매 봇 시작 중...
echo ======================================================
echo.

:: 1. .env 설정 파일 확인
if not exist ".env" (
    echo ❌ [오류] .env 설정 파일이 없습니다!
    echo    .env 파일을 생성하고 빗썸 API 키와 구글 시트 정보를 입력해 주세요.
    echo.
    pause
    exit /b 1
)

:: 2. 가상환경(venv) 존재 여부 확인 및 자동 구축
if not exist "venv\Scripts\python.exe" (
    echo 📦 [1/3] 파이썬 가상환경(venv)이 없어 새로 구축합니다...
    
    :: 시스템에 파이썬이 설치되어 있는지 확인
    where python >nul 2>nul
    if %errorlevel% neq 0 (
        where py >nul 2>nul
        if %errorlevel% neq 0 (
            echo.
            echo ❌ [오류] PC에 Python이 설치되어 있지 않거나 환경변수(PATH)에 등록되지 않았습니다!
            echo 👉 https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치해 주세요.
            echo    (※ 설치 시 반드시 'Add Python to PATH' 체크박스를 선택하세요!)
            echo.
            pause
            exit /b 1
        ) else (
            py -m venv venv
        )
    ) else (
        python -m venv venv
    )

    if not exist "venv\Scripts\python.exe" (
        echo ❌ 가상환경 생성 실패. 파이썬 설치 상태를 확인하세요.
        pause
        exit /b 1
    )

    echo 📥 [2/3] 필수 패키지 설치 중 (requests, PyJWT, gspread, apscheduler 등)...
    "%~dp0venv\Scripts\pip.exe" install -r "%~dp0requirements.txt"
    echo.
)

:: 3. logs 폴더 생성
if not exist "logs" mkdir "logs"

:: 4. 이미 실행 중인지 확인
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' };" ^
    "if ($procs) {" ^
    "    Write-Host 'ℹ️ 이미 봇이 백그라운드에서 실행 중입니다.' -ForegroundColor Cyan;" ^
    "} else {" ^
    "    Write-Host '🚀 [3/3] 봇 프로세스 시작 중...' -ForegroundColor Gray;" ^
    "    Start-Process -FilePath '%~dp0venv\Scripts\pythonw.exe' -ArgumentList '%~dp0src\main.py' -WorkingDirectory '%~dp0';" ^
    "    Start-Sleep -Seconds 2;" ^
    "}"

echo.
echo [실행 상태 점검]:
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$procs = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*src\main.py*' -or $_.CommandLine -like '*src/main.py*' };" ^
    "if ($procs) {" ^
    "    foreach ($p in $procs) {" ^
    "        $procObj = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue;" ^
    "        $memMB = [math]::Round($procObj.WorkingSet64 / 1MB, 2);" ^
    "        Write-Host ('🟢 [정상 실행 중] PID: ' + $p.ProcessId + ' | 메모리 점유: ' + $memMB + ' MB') -ForegroundColor Green;" ^
    "    }" ^
    "} else {" ^
    "    Write-Host '❌ 봇 시작에 실패했습니다! 직접 실행하여 에러 내용을 확인합니다:' -ForegroundColor Red;" ^
    "    & '%~dp0venv\Scripts\python.exe' '%~dp0src\main.py';" ^
    "}"

echo.
echo ======================================================
echo  📜 실시간 로그 모니터링 (Ctrl + C 누르면 닫힘)
echo  ※ 이 창을 닫아도 봇은 백그라운드에서 계속 24시간 돌아갑니다!
echo ======================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "while (!(Test-Path 'logs\trading.log')) { Write-Host '로그 파일 대기 중...'; Start-Sleep -Seconds 1 };" ^
    "Get-Content -Path 'logs\trading.log' -Wait -Tail 30 -Encoding UTF8"

pause
