@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Stop All Bots and Dashboard

venv\Scripts\python.exe src\process_manager.py all stop
pause
