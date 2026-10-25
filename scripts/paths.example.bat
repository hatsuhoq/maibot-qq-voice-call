@echo off
rem 本机路径配置示例。复制为同目录下的 paths.local.bat 后按实际安装位置修改；paths.local.bat 不会被提交。

rem GPT-SoVITS 整合包目录（包含 api_v2.py 与 runtime\python.exe）
set "GSV=C:\path\to\GPT-SoVITS"
rem NapCat 启动脚本，以及 NapCat 的 Shell 目录和其中 QQ 的 Files 目录（通话桥安装/启动用）
set "NAPCAT_BAT=C:\path\to\NapCat\启动NapCat.bat"
set "NAPCAT_SHELL_DIR=C:\path\to\NapCat\Shell"
set "NAPCAT_QQ_DIR=C:\path\to\NapCat\QQ\Files"
rem MaiBot 根目录（包含 bot.py 与 启动MaiBot.bat）
set "MAIBOT_DIR=C:\path\to\maibot"
rem Studio One 路由工程文件或所在文件夹；留空则在“文档\Studio One\Songs”里找名字含 通话/路由 的工程
set "STUDIO_ONE_SONG="
rem 可选：make-zh-refs.bat 使用的日语参考片段目录和中文参考音频输出目录
set "ZH_REF_JA_CLIPS="
set "ZH_REF_OUT_DIR="
