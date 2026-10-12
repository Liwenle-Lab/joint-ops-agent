@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 联合作战指挥多Agent系统 - 作战环境地图

echo ================================================================
echo   正在启动服务并打开「作战环境地图」
echo   浏览器会自动打开 http://127.0.0.1:8000/map
echo   不要关闭这个窗口（关掉 = 服务停止）
echo ================================================================
echo.

REM --- 找 Python：本机现成环境优先（依赖齐全），再到系统 python / py 启动器 ---
set "PY="
if exist "C:\Users\LE LE\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "PY=C:\Users\LE LE\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined PY python --version >nul 2>&1 && set "PY=python"
if not defined PY py --version >nul 2>&1 && set "PY=py"
if not defined PY (
    echo [错误] 本机找不到 Python。请到 https://www.python.org/downloads/ 安装 Python 3.10+，
    echo        安装时务必勾选 "Add Python to PATH"，装完重新双击本脚本。
    pause >nul
    exit /b 1
)

REM --- 若 8000 端口被上次没关干净的服务占用，先释放 ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo [提示] 端口 8000 已被进程 %%a 占用，正在释放...
    taskkill /F /PID %%a >nul 2>&1
)

REM --- 等服务起来后自动打开浏览器（延迟约 4 秒）---
start "" /b cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:8000/map"

"%PY%" server.py --port 8000

echo.
echo ----------------------------------------------------------------
echo 服务已停止。按任意键关闭窗口
pause >nul
