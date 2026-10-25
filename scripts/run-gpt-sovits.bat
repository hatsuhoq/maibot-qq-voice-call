@echo off
chcp 65001 >nul
call "%~dp0paths.bat" || exit /b 1
cd /d "%GSV%"
set "PATH=%GSV%\runtime;%PATH%"
"%GSV%\runtime\python.exe" api_v2.py -a 127.0.0.1 -p 9880
