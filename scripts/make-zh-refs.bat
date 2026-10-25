@echo off
chcp 65001 >nul
rem 用艾丽妮 v4 模型生成中文情绪参考音频，完成后自动切回原模型。运行期间不要打电话。
call "%~dp0paths.bat" || (pause & exit /b 1)
cd /d "%~dp0.."
call "%~dp0ensure-services.bat"
if not exist "scripts\logs" mkdir "scripts\logs"
set PYTHONUTF8=1
".venv\Scripts\python.exe" scripts\make_zh_refs.py > scripts\logs\make-zh-refs.log 2>&1
type scripts\logs\make-zh-refs.log
pause
