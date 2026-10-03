#!/usr/bin/env bash
# CommunityPulse 启动脚本（macOS / Linux）
# 用法：chmod +x 启动.sh && ./启动.sh
set -e

cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

echo "========================================================"
echo "  CommunityPulse 社区舆情雷达"
echo "========================================================"
echo

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "[错误] 没有检测到 Python。"
  echo "请先安装 Python 3.9 及以上版本，然后重新运行本脚本。"
  exit 1
fi
echo "检测到 $PY：$($PY --version 2>&1)"

if [ ! -x "venv/bin/python" ]; then
  echo
  echo "[首次运行] 正在创建虚拟环境 venv ..."
  $PY -m venv venv
fi

VENV_PY="venv/bin/python"

if [ ! -f "venv/.deps_installed" ]; then
  echo
  echo "[首次运行] 正在安装可选依赖，约需 1-2 分钟 ..."
  echo "  装不上也不会影响使用，程序会自动降级。"
  echo
  # 关掉 set -e：pip 装不上不应该中断启动，程序会降级运行
  set +e
  if ls wheels/*.whl >/dev/null 2>&1; then
    echo "检测到离线依赖包 wheels/，使用离线安装（不需要联网）..."
    "$VENV_PY" -m pip install --no-index --find-links=wheels -r requirements.txt
  else
    "$VENV_PY" -m pip install -r requirements.txt
  fi
  RC=$?
  set -e
  if [ "$RC" -eq 0 ]; then
    touch "venv/.deps_installed"
    echo "依赖安装完成。"
  else
    echo
    echo "[提示] 依赖安装失败（常见于网络受限）。"
    echo "  程序仍可运行：分词用内置方案、Excel 导出降级为 CSV。"
  fi
fi

echo
echo "正在启动 CommunityPulse ..."
echo "启动后请勿关闭本窗口，关闭窗口即停止服务。"
echo
exec "$VENV_PY" app/main.py "$@"
