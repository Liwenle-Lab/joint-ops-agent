@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 联合作战指挥多Agent系统 - 全部回归

echo ================================================================
echo   全部回归：静态检查 + 13 组测试
echo.
echo   会依次跑：
echo     1. 静态检查（专抓"改了代码却漏导入"这类一跑必炸的错）
echo     2. 全链路验收用例（MOCK 模式，不花钱）
echo     3~7. 后端单测（环境 / 增量 / 协商 / 反馈 / 兵力 / 链路）
echo     8~12. 界面测试（会自动开几次无头浏览器，属正常现象）
echo.
echo   全程约 5~10 分钟，中途不要关这个窗口。
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

"%PY%" tests\run_all.py
set RC=%ERRORLEVEL%

echo.
echo ----------------------------------------------------------------
if "%RC%"=="0" (
    echo 全部通过。
) else (
    echo 有失败项（退出码 %RC%）。请往上翻，看带 ✗ 的那几行。
)
echo ----------------------------------------------------------------
echo 按任意键关闭窗口
pause >nul
