"""全局配置：所有路径固定在 E 盘项目目录内。"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
LOG_DIR = ROOT / "logs"
DICT_DIR = ROOT / "app" / "analysis" / "dict"

for _d in (DATA_DIR, LOG_DIR, DICT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "pulse.db"
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("PULSE_HOST", "127.0.0.1")
PORT = int(os.getenv("PULSE_PORT", "8766"))

# 发送真实请求时的默认头，尽量模拟常规浏览器
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/json,application/xhtml+xml,*/*;q=0.8",
}

REQUEST_TIMEOUT = 20.0
REQUEST_RETRY = 2
# 采集节流：每页之间的最小间隔（秒），降低被风控概率
PAGE_DELAY = 0.8

# 单次任务默认最大抓取条数上限（防止误操作拉爆）
MAX_COMMENTS_PER_TASK = 20000


def load_settings() -> dict:
    """用户级设置：Cookie / API Key / 代理等，保存在 data/settings.json。"""
    p = DATA_DIR / "settings.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(data: dict) -> None:
    p = DATA_DIR / "settings.json"
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
