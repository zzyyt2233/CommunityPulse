@echo off
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
