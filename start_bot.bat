@echo off
chcp 65001 > nul
cd /d "%~dp0"

echo [1/2] 가상환경(venv) 및 봇 실행 준비 중...

if not exist "venv\Scripts\python.exe" (
    echo 가상환경(venv)이 존재하지 않아 새로 생성합니다...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
)

echo [2/2] 빗썸 자동매매 봇을 무소음 백그라운드 프로세스로 시작합니다...
powershell -Command "Start-Process -WindowStyle Hidden -FilePath '%~dp0venv\Scripts\pythonw.exe' -ArgumentList '%~dp0src\main.py' -WorkingDirectory '%~dp0'"

echo.
echo ======================================================
echo  🚀 봇이 백그라운드에서 실행되었습니다! (RAM 약 40MB 점유)
echo  로그는 텔레그램 및 구글 스프레드시트에서 실시간 확인 가능합니다.
echo  봇을 종료하려면 stop_bot.bat 을 실행하세요.
echo ======================================================
timeout /t 3 > nul
