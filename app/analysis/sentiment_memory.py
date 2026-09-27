"""情感标签记忆（用户纠错后的持久化情感词典）。

用户可在界面上把某个「词条」的正面 / 中立 / 负面标签改掉，这里负责把它
持久化下来，并在后续每次分析时生效——这就是「形成记忆，下次查询到后正确标记」。

存储：data/sentiment_memory.json
结构：{ word: {"label": "positive|neutral|negative", "weight": float,
               "by": str, "at": float(时间戳)} }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from ..core.config import DATA_DIR

MEMORY_FILE = DATA_DIR / "sentiment_memory.json"

# 记忆标签对应的词典权重（与 lexicon.POSITIVE / NEGATIVE 同量级，足以主导判定）
_POS_W = 2.5
_NEG_W = -2.5
_VALID = ("positive", "neutral", "negative")


def load_memory() -> dict[str, dict]:
    """读取情感记忆表；文件不存在或损坏时返回空表。"""
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


def set_label(word: str, label: str, by: str = "user") -> dict[str, dict]:
    """设置 / 更新一个词条的记忆标签。

    label 为 neutral 时视为「该词不具情感」，写入 ignore 列表（分析时跳过它）。
    返回更新后的该词条记忆条目。
    """
    word = (word or "").strip().lower()
    if not word:
        raise ValueError("词条为空")
    if label not in _VALID:
        raise ValueError(f"非法标签：{label}")
    memory = load_memory()
    if label == "neutral":
        weight = 0.0
    elif label == "positive":
        weight = _POS_W
    else:
        weight = _NEG_W
    memory[word] = {
        "label": label,
        "weight": weight,
        "by": by,
        "at": time.time(),
    }
    _save(memory)
    return memory[word]


def delete_label(word: str) -> None:
    """删除一个词条的记忆（恢复默认词典判定）。"""
    word = (word or "").strip().lower()
    memory = load_memory()
    memory.pop(word, None)
    _save(memory)


def overrides_to_lexicon(memory: dict[str, dict] | None = None
                        ) -> dict[str, dict]:
    """把记忆表转成 score_text 需要的覆盖项。

    返回 {"pos": {word: weight}, "neg": {word: weight}, "ignore": {word}}。
    正面 / 负面词注入到 pos / neg；中立词进 ignore（分析时直接跳过，避免误判）。
    """
    memory = load_memory() if memory is None else memory
    pos: dict[str, float] = {}
    neg: dict[str, float] = {}
    ignore: set[str] = set()
    for w, info in memory.items():
        label = (info or {}).get("label")
        if label == "positive":
            pos[w] = float((info or {}).get("weight", _POS_W) or _POS_W)
        elif label == "negative":
            neg[w] = float((info or {}).get("weight", _NEG_W) or _NEG_W)
        elif label == "neutral":
            ignore.add(w)
    return {"pos": pos, "neg": neg, "ignore": ignore}


def auto_label_of(word: str, pos: dict, neg: dict) -> str:
    """词条在默认词典里的自动极性（用于展示「自动判定」基线）。"""
    if word in pos:
        return "positive"
    if word in neg:
        return "negative"
    return "neutral"


def memory_label_of(word: str, memory: dict[str, dict]) -> Optional[str]:
    """该词条在记忆表里的标签（None 表示未人工设定）。"""
    info = memory.get(word)
    return info.get("label") if info else None
