"""评论 / 语句级情感记忆。

你在界面上把某条「评论 / 语句」标成正面 / 中立 / 负面后，这里按归一化正文
把它记下来；下次同样内容的评论再次被采集、或对同一任务重新分析时，直接沿用
你的标签。这就是「改标签并形成记忆，下次查询到后正确标记」。

为什么不按评论 ID 记：同一条内容可能来自不同平台 / 不同任务，评论 ID 会变，
而正文不会变。所以按「归一化正文」做指纹，跨来源都能命中。

存储：data/comment_memory.json
结构：{ fingerprint: {"label": "positive|neutral|negative",
                      "text": 原文片段, "by": "user", "at": 时间戳} }
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Optional

from ..core.config import DATA_DIR
from .text_utils import clean_emoji_stickers

MEMORY_FILE = DATA_DIR / "comment_memory.json"

_VALID = ("positive", "neutral", "negative")
# 归一化：只保留中英文与数字，其余（标点 / 空白）全部忽略
_NORM_RE = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def norm_text(text: str) -> str:
    """把评论归一化成可比较的骨架。

    先整段剥掉表情 / 贴纸码（否则 [doge] 会残留成 "doge"，同一条评论
    带不带贴纸就变成两个指纹），再去标点、空格并统一小写。
    """
    cleaned = clean_emoji_stickers(text or "")
    return _NORM_RE.sub("", cleaned.lower())


def norm_key(text: str) -> str:
    """评论指纹：归一化正文的短哈希。空文本返回空串。"""
    n = norm_text(text)
    if not n:
        return ""
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:16]


def load_memory() -> dict[str, dict]:
    """读取语句记忆表；文件不存在或损坏时返回空表。"""
    if not MEMORY_FILE.exists():
        return {}
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save(memory: dict[str, dict]) -> None:
    MEMORY_FILE.write_text(
        json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def save(key: str = "", label: str = "", text: str = "", by: str = "user") -> dict:
    """记住一条评论 / 语句的情感标签。

    key 优先（分析结果里已算好，避免前端截断文本导致指纹不一致）；
    key 为空时用 text 现算。
    """
    key = (key or "").strip() or norm_key(text)
    if not key:
        raise ValueError("评论内容为空，无法记忆")
    if label not in _VALID:
        raise ValueError(f"非法标签：{label}")
    memory = load_memory()
    memory[key] = {
        "label": label,
        "text": (text or "").strip()[:120],
        "by": by,
        "at": time.time(),
    }
    _save(memory)
    return memory[key]


def label_of(memory: dict[str, dict] | None, key: str) -> Optional[str]:
    """查某条评论在记忆表里的标签（None 表示没人工标过）。"""
    info = (memory or {}).get(key or "")
    return info.get("label") if info else None


def get_label(text: str, memory: dict[str, dict] | None = None) -> Optional[str]:
    """按正文查记忆标签。"""
    mem = load_memory() if memory is None else memory
    return label_of(mem, norm_key(text))


def remove(key: str) -> bool:
    """撤销一条语句记忆（恢复自动判定）。"""
    key = (key or "").strip()
    memory = load_memory()
    if key and key in memory:
        memory.pop(key, None)
        _save(memory)
        return True
    return False


def count() -> int:
    return len(load_memory())
