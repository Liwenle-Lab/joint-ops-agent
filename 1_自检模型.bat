@echo off
chcp 65001 >nul
cd /d "%~dp0"

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
echo ================================================================
echo   第 1 步：自检模型配置（连通性 + JSON 输出）
echo ================================================================
echo.
"%PY%" check_llm.py
echo.
echo ----------------------------------------------------------------
echo 看完结果按任意键关闭窗口
pause >nul
