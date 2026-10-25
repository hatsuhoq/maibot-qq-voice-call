@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
".venv\Scripts\python.exe" scripts\route_call_audio.py > scripts\logs\route-call-audio.log 2>&1
type scripts\logs\route-call-audio.log
pause
