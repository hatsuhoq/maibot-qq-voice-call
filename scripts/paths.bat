@echo off
rem 读取本机路径配置 scripts\paths.local.bat（不提交到仓库）。首次使用：复制 paths.example.bat 为 paths.local.bat 并按实际安装位置填写。
if not exist "%~dp0paths.local.bat" (
  echo ★缺少 scripts\paths.local.bat：请复制 scripts\paths.example.bat 为 paths.local.bat，并填写本机路径。
  exit /b 1
)
call "%~dp0paths.local.bat"
