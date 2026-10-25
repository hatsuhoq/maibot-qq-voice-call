@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
".venv\Scripts\python.exe" scripts\call_diag.py > scripts\logs\call-diag.log 2>&1
type scripts\logs\call-diag.log
pause
