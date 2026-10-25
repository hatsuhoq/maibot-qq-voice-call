@echo off
chcp 65001 >nul
call "%~dp0paths.bat" || exit /b 1
cd /d "%GSV%"
set "PATH=%GSV%\runtime;%PATH%"
"%GSV%\runtime\python.exe" "%~dp0..\services\funasr_server.py" --gsv-root "%GSV%" --device cuda:0
