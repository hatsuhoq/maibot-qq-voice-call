@echo off
chcp 65001 >nul
rem One-click start. Local paths come from scripts\paths.local.bat.
call "%~dp0paths.bat" || (pause & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-all.ps1"
pause
