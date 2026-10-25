@echo off
chcp 65001 >nul
setlocal
call "%~dp0ensure-services.bat"
cd /d "%~dp0.."
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
echo 正在自检：会等待服务加载（最多 3 分钟），并播放约 3 秒很轻的提示音测试虚拟声卡……
".venv\Scripts\python.exe" scripts\selfcheck.py > scripts\logs\selfcheck.log 2>&1
type scripts\logs\selfcheck.log
echo.
echo 日志已保存到 scripts\logs\selfcheck.log，试合成的声音在 scripts\logs\tts_*.wav。
pause
