"""生成 启动.bat / 停止.bat —— 这两个文件的唯一生成入口。

Windows 批处理有两个硬坑，手写必踩：
1. 换行必须是 CRLF，裸 LF 会让命令边界错乱（症状是「不是内部或外部命令」）。
2. 含中文必须用 GBK 编码（cmd 默认代码页 936），UTF-8 会乱码甚至破坏语法。

要改启动流程就改这里，不要直接编辑根目录的 .bat —— 否则下次有人跑本脚本
会把改动冲掉；反过来，直接改 .bat 而不改这里，两边也会慢慢分叉。

用法：python tools/make_bat.py
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 注意 URI 里的 % 必须写成 %%，因为 batch 会把它当变量展开
START = r'''@echo off
title CommunityPulse 社区舆情雷达
cd /d "%~dp0"

set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ========================================================
echo   CommunityPulse 社区舆情雷达
echo ========================================================
echo.

REM ---------- 1. 找 Python：优先用随包自带的运行时 ----------
set PY=
if exist "runtime\py\python.exe" (
  set PY=runtime\py\python.exe
  echo 使用随包自带的 Python 运行时，无需你安装 Python，也无需联网。
)

if not defined PY (
  where python >nul 2>&1
  if errorlevel 1 (
    echo [错误] 没有检测到 Python。
    echo.
    echo 请先安装 Python 3.9 及以上版本（安装时务必勾选
    echo "Add Python to PATH"），然后重新双击本文件。
    echo 下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
  )
  for /f "tokens=2" %%v in ('python -c "import sys;print(sys.version.split()[0])"') do set PYVER=%%v
  echo 检测到系统 Python %PYVER%
)

REM ---------- 2. 没有自带运行时时：建虚拟环境 ----------
if not defined PY (
  if not exist "venv\Scripts\python.exe" (
    echo.
    echo [首次运行] 正在创建虚拟环境 venv ...
    python -m venv venv
    if errorlevel 1 (
      echo [错误] 虚拟环境创建失败，请确认 Python 安装完整。
      pause
      exit /b 1
    )
  )
  set PY=venv\Scripts\python.exe
)

REM ---------- 3. 没有自带运行时时：装依赖（可选，装不上也能降级运行） ----------
if not defined PY goto :READY
if "%PY%"=="runtime\py\python.exe" goto :READY

if not exist "venv\.deps_installed" (
  echo.
  echo [首次运行] 正在安装可选依赖，约需 1-2 分钟 ...
  echo   装不上也不会影响使用，程序会自动降级。
  echo.
  if exist wheels\*.whl (
    echo 检测到离线依赖包 wheels\，使用离线安装（不需要联网）...
    "%PY%" -m pip install --no-index --find-links=wheels -r requirements.txt
  ) else (
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r requirements.txt
  )
  if not errorlevel 1 (
    echo.>"venv\.deps_installed"
    echo 依赖安装完成。
  ) else (
    echo.
    echo [提示] 依赖安装失败（常见于网络受限）。
    echo   程序仍可运行：分词用内置方案、Excel 导出降级为 CSV。
    echo   需要联网重试时，重新运行本脚本即可。
  )
)

:READY

REM ---------- 4. 启动服务 ----------
echo.
echo 正在启动 CommunityPulse ...
echo 启动后请勿关闭本窗口，关闭窗口即停止服务。
echo.

"%PY%" app\main.py %1

echo.
echo 服务已停止。按任意键关闭窗口。
pause >nul
'''

STOP = r'''@echo off
title 停止 CommunityPulse
cd /d "%~dp0"

set PY=runtime\py\python.exe
if not exist "%PY%" if exist "venv\Scripts\python.exe" set PY=venv\Scripts\python.exe
if not exist "%PY%" set PY=python

REM 优先按 data\instance.json 里记录的进程号精确停止，
REM 避免像以前那样无差别杀掉所有 87xx 端口的进程（会误伤本机其它服务）。
"%PY%" tools\stop_instance.py %1

if errorlevel 1 (
  echo.
  echo 未能自动停止，请回到 CommunityPulse 的黑窗口按 Ctrl+C。
)

echo.
REM 不用 timeout：某些环境里 PATH 上有同名命令（如 Git 的 timeout）会把它抢掉。
pause
'''


def write(name: str, text: str) -> None:
    # 统一 LF -> CRLF，再按 GBK 落盘
    data = text.replace("\n", "\r\n").encode("gbk")
    (ROOT / name).write_bytes(data)
    print(f"written {name} ({len(data)} bytes, CRLF+GBK)")


if __name__ == "__main__":
    write("启动.bat", START)
    write("停止.bat", STOP)
