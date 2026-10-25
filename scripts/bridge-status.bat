@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
".venv\Scripts\python.exe" scripts\bridge_status.py > scripts\logs\bridge-status.log 2>&1
type scripts\logs\bridge-status.log
pause
