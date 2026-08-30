@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Unified Dashboard Server Status

venv\Scripts\python.exe src\process_manager.py dashboard status
pause
