@echo off
chcp 65001 >nul
call "%~dp0ensure-services.bat"
echo.
echo 两个服务窗口出现“就绪/Uvicorn running”后即可使用。不要关闭服务窗口。
pause
