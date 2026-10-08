"""全局配置：所有路径固定在 E 盘项目目录内。"""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import secret
from .credentials import ACCESS

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

# 需要加密落盘的字段名。由平台注册表驱动（有的平台 setting 是空串，过滤掉），
# 以后加平台只改 credentials.ACCESS，这里自动跟上 —— 不会漏掉新平台的 Cookie。
_SECRET_KEYS = {str(a.get("setting") or "") for a in ACCESS} - {""}

# load_settings 时解不开的密文对应字段名（正常为空），启动时提示用户重配
_lost_secrets: list[str] = []


def _is_secret_key(key: str) -> bool:
    """这个字段名看起来是不是凭据？

    名单由平台注册表（credentials.ACCESS）驱动 —— 以后加平台只会改那张表，
    这里自动跟上，不会出现「新平台的 Cookie 忘记加密」。
    """
    if key in _SECRET_KEYS:
        return True
    low = key.lower()
    return any(h in low for h in ("cookie", "token", "secret", "password", "passwd"))


def _walk(obj, fn):
    """按字段名加解密：只动「看起来是凭据」的字符串，其余原样。

    递归是必要的 —— 以后设置里出现嵌套结构（比如带认证的代理）也能覆盖到。
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, str) and _is_secret_key(str(k)):
                out[k] = fn(str(k), v)
            else:
                out[k] = _walk(v, fn)
        return out
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    return obj


def _encrypt_field(_key: str, value: str) -> str:
    return secret.protect(value)


def _decrypt_field(key: str, value: str) -> str:
    if not value:
        return value
    plain = secret.unprotect(value)
    if not plain and secret.is_encrypted(value):
        # 密文解不开：换了机器或换了 Windows 账户。记下来，启动时提示重配。
        _lost_secrets.append(key)
    return plain


def lost_secrets() -> list[str]:
    """上一次 load_settings 时解不开的凭据字段名（正常为空）。"""
    return list(_lost_secrets)


def load_settings() -> dict:
    """用户级设置：Cookie / API Key / 代理等，保存在 data/settings.json。

    盘上凭据是 DPAPI 密文，这里解密后返回明文 —— 调用方（采集器）照旧直接用。
    """
    _lost_secrets.clear()
    p = DATA_DIR / "settings.json"
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return _walk(raw, _decrypt_field)


def save_settings(data: dict) -> None:
    """写盘前把凭据字段加密。非凭据字段（阈值、语言、排序等）原样存，方便排查。"""
    p = DATA_DIR / "settings.json"
    p.write_text(json.dumps(_walk(data or {}, _encrypt_field),
                            ensure_ascii=False, indent=2), encoding="utf-8")
