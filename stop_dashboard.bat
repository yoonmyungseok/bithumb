@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Stop Unified Dashboard Server

venv\Scripts\python.exe src\process_manager.py dashboard stop
pause
