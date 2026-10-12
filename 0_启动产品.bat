@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 联合作战指挥多Agent系统 - 产品启动台

echo ================================================================
echo   联合作战指挥多Agent系统 · 产品启动台
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

REM --- 首跑自动准备：缺依赖就装，缺数据库就建 ---
"%PY%" -c "import fastapi, langgraph, dotenv" >nul 2>&1
if errorlevel 1 (
    echo [准备] 首次运行，正在安装依赖（几分钟，只需一次）...
    "%PY%" -m pip install -r requirements.txt
)
if not exist "db\joint_ops.db" (
    echo [准备] 数据库不存在，正在从种子数据创建...
    "%PY%" db\build_db.py
)

REM --- 若 8000 端口被上次没关干净的服务占用，先释放 ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo [提示] 端口 8000 已被进程 %%a 占用，正在释放...
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 >nul 2>&1

REM --- 友好提示：地图用的是正式 key 还是体验额度 ---
"%PY%" -c "import os;from dotenv import load_dotenv;load_dotenv('config/.env');k=os.getenv('TENCENT_MAP_KEY','');print('[地图] 已配置正式腾讯地图 Key' if k else '[地图] 未检测到 Key，地图将显示「体验额度」版（功能正常，仅底图有水印）')" 2>nul

echo.
echo 正在启动服务，请稍候...
echo 启动后浏览器会自动打开「产品首页」。
echo 不要关闭这个黑窗口（关掉 = 服务停止）。
echo.

REM --- 等服务起来后自动打开浏览器（延迟约 5 秒）---
start "" /b cmd /c "ping -n 6 127.0.0.1 >nul & start http://127.0.0.1:8000/home"

"%PY%" server.py --port 8000

echo.
echo ----------------------------------------------------------------
echo 服务已停止。按任意键关闭窗口
pause >nul
