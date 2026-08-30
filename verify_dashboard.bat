@echo off
setlocal

set "DASHBOARD_DIR=%~dp0dashboard"
if not exist "%DASHBOARD_DIR%\node_modules" (
    echo Dashboard dependencies are missing. Run "npm install" in "%DASHBOARD_DIR%" first.
    exit /b 1
)

pushd "%DASHBOARD_DIR%"
call npm run build
set "EXIT_CODE=%ERRORLEVEL%"
popd
exit /b %EXIT_CODE%
