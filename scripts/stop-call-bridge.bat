@echo off
chcp 65001 >nul
rem 只结束 AV Host（第二个 QQ 进程），不会影响 NapCat 和小睦。
call "%~dp0paths.bat" || (pause & exit /b 1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0..\bridge\windows.ps1" -Action stop
pause
