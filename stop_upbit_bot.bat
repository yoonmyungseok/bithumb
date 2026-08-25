@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Stop Upbit Bot
venv\Scripts\python.exe src\process_manager.py upbit stop
timeout /t 2 > nul
