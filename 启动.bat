@echo off
chcp 65001 >nul
title vocab-harvester 词库采集系统
echo ============================================================
echo   vocab-harvester - 社交平台词库采集系统
echo ============================================================
echo.
echo 正在启动服务...
echo 启动后请勿关闭此窗口！
echo.
cd /d "%~dp0"
python start.py
pause
