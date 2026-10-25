@echo off
chcp 65001 >nul
rem 启动独立的 AV Host（第二个 QQ 进程，只加载通话组件，不登录界面）。需在 NapCat 已运行时启动。
call "%~dp0paths.bat" || (pause & exit /b 1)
cd /d "%~dp0.."
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
rem 每次用新的日志文件名：上一次的 QQ 子进程可能仍占用旧文件。
set "LOG=scripts\logs\start-bridge-%RANDOM%%RANDOM%.log"
echo 正在启动通话组件，约需 15 秒……
powershell -NoProfile -ExecutionPolicy Bypass -File "bridge\windows.ps1" -Action start > "%LOG%" 2>&1
echo 退出码 %errorlevel% >> "%LOG%"
type "%LOG%"
timeout /t 10 >nul
".venv\Scripts\python.exe" scripts\bridge_status.py > scripts\logs\bridge-status.log 2>&1
type scripts\logs\bridge-status.log
pause
