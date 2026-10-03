#!/usr/bin/env bash
# 停止 CommunityPulse（macOS / Linux）
# 按 data/instance.json 精确终止，不会误伤本机其它服务。
cd "$(dirname "$0")"

if [ -x "venv/bin/python" ]; then PY="venv/bin/python"; else PY="python3"; fi
exec "$PY" tools/stop_instance.py "$@"
