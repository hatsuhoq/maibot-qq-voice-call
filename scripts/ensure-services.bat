@echo off
rem 被其他脚本调用：启动 GPT-SoVITS（9880）和 FunASR（8091）；已经在运行的会跳过。
netstat -ano | findstr /r /c:"127.0.0.1:9880 .*LISTENING" /c:"0.0.0.0:9880 .*LISTENING" >nul
if errorlevel 1 (
  echo 启动 GPT-SoVITS 合成服务（端口 9880）……
  start "GPT-SoVITS 9880" cmd /k call "%~dp0run-gpt-sovits.bat"
) else (
  echo GPT-SoVITS 已在 9880 端口运行。
)
netstat -ano | findstr /r /c:"127.0.0.1:8091 .*LISTENING" >nul
if errorlevel 1 (
  echo 启动 FunASR 识别服务（端口 8091）……
  start "FunASR 8091" cmd /k call "%~dp0run-funasr.bat"
) else (
  echo FunASR 已在 8091 端口运行。
)
