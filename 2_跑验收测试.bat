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
echo   第 2 步：跑第一期验收用例（6 条）
echo   注意看开头显示 [MOCK] 还是 [真实模型]
echo ================================================================
echo.
"%PY%" run.py --test
echo.
echo ----------------------------------------------------------------
echo 看完结果按任意键关闭窗口
pause >nul
