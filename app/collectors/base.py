"""采集器抽象与注册中心。"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

# 可靠性等级
STABLE = "stable"        # 官方/半官方公开接口，长期可用
BEST_EFFORT = "best-effort"  # 网页结构解析，可能随改版失效
RESTRICTED = "restricted"    # 有风控/需登录，成功率不稳定
MANUAL = "manual"        # 只能人工导入


@dataclass
class Comment:
    comment_id: str = ""
    parent_id: str = ""
    user_name: str = ""
    content: str = ""
    like_count: int = 0
    reply_count: int = 0
    published_at: float | None = None
    url: str = ""
    extra: dict = field(default_factory=dict)

    def is_valid(self) -> bool:
        return bool(self.content and self.content.strip())


@dataclass
class CollectResult:
    platform: str
    source_id: str = ""
    source_title: str = ""
    source_url: str = ""
    comments: list[Comment] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reliability: str = BEST_EFFORT
    # 抓取诊断：目标条数、是否抓满、没抓满的原因
    requested: int = 0
    complete: bool = True
    stop_reason: str = ""
    # 平台侧该内容的评论总数（接口有返回时填充，用于"目标/实得/总量"三元组展示）
    available: int = 0
    # 本次接口实际允许的上限（用于提示登录/风控限制）
    capacity_hint: str = ""

    def to_rows(self) -> list[dict]:
        return [
            {
                "platform": self.platform,
                "source_id": self.source_id,
                "comment_id": c.comment_id,
                "parent_id": c.parent_id,
                "user_name": c.user_name,
                "content": c.content,
                "like_count": c.like_count,
                "reply_count": c.reply_count,
                "published_at": c.published_at,
                "url": c.url,
                "extra": c.extra,
            }
            for c in self.comments
            if c.is_valid()
        ]


class BaseCollector:
    platform = "generic"
    label = "通用"
    reliability = BEST_EFFORT
    # 该平台的 URL 特征
    patterns: tuple[str, ...] = ()
    # 使用说明（展示在界面上）
    hint = ""

    def can_handle(self, url: str) -> bool:
        return any(re.search(p, url, re.I) for p in self.patterns)

    def extract_id(self, url: str) -> str:
        for p in self.patterns:
            m = re.search(p, url, re.I)
            if m and m.groups():
                return m.group(1)
        return ""

    def collect(self, url: str, limit: int = 500,
                progress: Callable[[int, str], None] | None = None,
                settings: dict | None = None, sort: str = "hot") -> CollectResult:
        """sort: hot=按热度优先, new=按时间优先, both=两路合并（抓得更全）。"""
        raise NotImplementedError

    # 子类可用的小工具
    @staticmethod
    def _tick(progress, got: int, msg: str) -> None:
        if progress:
            progress(got, msg)


_REGISTRY: list[BaseCollector] = []


def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    _REGISTRY.append(cls())
    return cls


def all_collectors() -> list[BaseCollector]:
    return list(_REGISTRY)


def route(url: str) -> BaseCollector | None:
    for c in _REGISTRY:
        if c.platform == "generic":
            continue
        if c.can_handle(url):
            return c
    for c in _REGISTRY:
        if c.platform == "generic":
            return c
    return None


def now_ts() -> float:
    return time.time()


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = (
        s.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def dedup(items: Iterable[Comment]) -> list[Comment]:
    seen: set[str] = set()
    out: list[Comment] = []
    for c in items:
        key = c.comment_id or (c.user_name + "|" + c.content[:80])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out
