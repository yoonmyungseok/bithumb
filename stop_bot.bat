@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Stop Bithumb Bot
venv\Scripts\python.exe src\process_manager.py bithumb stop
timeout /t 2 > nul
