"""通用网页抽取 + 手动导入（文本 / CSV / JSON）。

这是所有平台的兜底通道：任何链接抓不到结构化评论时，仍能把页面正文
切成候选句子；任何人工导出的评论文件也能直接喂进来。
"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Callable

from ..core.http_client import get_text
from .base import (
    BEST_EFFORT,
    MANUAL,
    BaseCollector,
    CollectResult,
    Comment,
    clean_text,
    dedup,
    register,
)

# 常见评论容器特征
CONTAINER_RE = re.compile(
    r"<(div|li|article|section|p)\b[^>]*?(?:class|id|data-[a-z]+)=[\"'][^\"']*?"
    r"(comment|reply|review|post|discuss|feedback|评价|评论|留言|回复)[^\"']*[\"'][^>]*>(.*?)</\1>",
    re.I | re.S,
)

BLOCK_RE = re.compile(r"<(p|div|li|article|section|span|td)\b[^>]*>(.*?)</\1>", re.I | re.S)

NOISE_RE = re.compile(
    r"(javascript|cookie|登录|注册|版权|©|all rights|隐私|条款|备案|ICP|"
    r"扫一扫|关注我们|举报|分享到|复制链接|展开更多|查看更多|加载中)",
    re.I,
)

CJK_RE = re.compile(r"[\u4e00-\u9fa5]")


def _strip_html(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<noscript[\s\S]*?</noscript>", " ", html, flags=re.I)
    html = re.sub(r"<!--[\s\S]*?-->", " ", html)
    return html


def _looks_like_comment(t: str) -> bool:
    t = t.strip()
    if not (4 <= len(t) <= 500):
        return False
    if NOISE_RE.search(t):
        return False
    if not CJK_RE.search(t) and not re.search(r"[A-Za-z]{3,}", t):
        return False
    # 纯链接 / 纯符号
    if re.fullmatch(r"[\W\d_]+", t):
        return False
    # 排除明显是导航的句子（无标点结尾的长菜单）
    if len(t.split()) > 60:
        return False
    return True


@register
class GenericCollector(BaseCollector):
    platform = "generic"
    label = "通用网页"
    reliability = BEST_EFFORT
    patterns = (r"^https?://",)
    hint = "任意链接：自动定位评论区容器并切分文本；抓不到结构化数据时退化为正文候选句。"

    def collect(self, url, limit=500, progress=None, settings=None, sort='hot'):
        res = CollectResult(self.platform, "", "", url, reliability=self.reliability,
                            requested=limit)
        html = get_text(url)
        if not html:
            res.warnings.append("页面为空或请求被拦截")
            return res
        m = re.search(r"<title>(.*?)</title>", html, re.S)
        if m:
            res.source_title = clean_text(m.group(1))[:100]
        body = _strip_html(html)

        found: list[str] = []
        for _, inner in CONTAINER_RE.findall(body):
            txt = clean_text(inner)
            if _looks_like_comment(txt):
                found.append(txt)
        if len(found) < 5:
            for _, inner in BLOCK_RE.findall(body):
                txt = clean_text(inner)
                if _looks_like_comment(txt):
                    found.append(txt)

        seen: set[str] = set()
        idx = 0
        for t in found:
            if t in seen:
                continue
            seen.add(t)
            idx += 1
            res.comments.append(Comment(comment_id=f"gen_{idx}", content=t, url=url))
            if len(res.comments) >= limit:
                break
        if not res.comments:
            res.warnings.append("未从页面中解析出评论内容，建议改用手动导入。")
            res.stop_reason = "页面未解析出评论结构（可能是动态渲染或需要登录）"
        self._tick(progress, len(res.comments), "通用抽取完成")
        res.comments = dedup(res.comments)[:limit]
        res.complete = len(res.comments) >= limit
        if not res.complete and not res.stop_reason:
            res.stop_reason = "页面可提取的评论不足目标条数"
        res.capacity_hint = "通用抽取按页面顺序，无点赞数据"
        return res


@register
class ManualCollector(BaseCollector):
    platform = "manual"
    label = "手动导入"
    reliability = MANUAL
    patterns = ()
    hint = "直接粘贴评论文本（一行一条），或上传 CSV/JSON；适合抖音、小红书等强风控平台。"

    def collect(self, url, limit=500, progress=None, settings=None, sort='hot'):
        raise RuntimeError("手动导入请通过导入接口提交内容")


# ---------------- 导入解析 ----------------

_FIELD_ALIASES = {
    "content": "content", "评论": "content", "内容": "content", "text": "content",
    "评论内容": "content", "正文": "content", "review": "content", "comment": "content",
    "user": "user_name", "用户名": "user_name", "作者": "user_name", "昵称": "user_name",
    "user_name": "user_name", "author": "user_name",
    "like": "like_count", "点赞": "like_count", "点赞数": "like_count",
    "like_count": "like_count", "赞": "like_count", "votes_up": "like_count",
    "time": "published_at", "时间": "published_at", "发布时间": "published_at",
    "date": "published_at", "created_at": "published_at", "timestamp": "published_at",
    "reply": "reply_count", "回复": "reply_count", "回复数": "reply_count",
}


def _norm_field(name: str) -> str:
    n = (name or "").strip().lower()
    return _FIELD_ALIASES.get(n, _FIELD_ALIASES.get(n.strip(), n))


def parse_time_any(v) -> float | None:
    import time as _t
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        v = float(v)
        if v > 1e12:
            v /= 1000.0
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y年%m月%d日", "%m/%d/%Y"):
        try:
            return _t.mktime(_t.strptime(s, fmt))
        except Exception:
            continue
    try:
        return float(s)
    except Exception:
        return None


def parse_import(raw: str, fmt: str = "auto") -> list[dict]:
    """把粘贴/上传的内容解析成评论行。fmt: auto|text|csv|json"""
    raw = (raw or "").strip()
    if not raw:
        return []
    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        s = raw.lstrip()
        if s.startswith("[") or s.startswith("{"):
            fmt = "json"
        elif ("," in raw.splitlines()[0] and raw.count('"') + raw.count(",") > 3) or "\t" in raw:
            fmt = "csv"
        else:
            fmt = "text"
    rows: list[dict] = []
    if fmt == "json":
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        items: list = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for k in ("comments", "data", "list", "items", "reviews", "results"):
                if isinstance(data.get(k), list):
                    items = data[k]
                    break
            else:
                items = [data]
        for i, it in enumerate(items):
            if isinstance(it, str):
                rows.append({"comment_id": f"imp_{i}", "content": it})
            elif isinstance(it, dict):
                d = {_norm_field(k): v for k, v in it.items()}
                if "content" not in d:
                    continue
                rows.append({
                    "comment_id": str(d.get("id") or d.get("comment_id") or f"imp_{i}"),
                    "user_name": str(d.get("user_name") or ""),
                    "content": str(d["content"]),
                    "like_count": int(d.get("like_count") or 0),
                    "reply_count": int(d.get("reply_count") or 0),
                    "published_at": parse_time_any(d.get("published_at")),
                })
    elif fmt == "csv":
        delim = "\t" if "\t" in raw.splitlines()[0] else ","
        reader = csv.DictReader(io.StringIO(raw), delimiter=delim)
        for i, r in enumerate(reader):
            d = {_norm_field(k): v for k, v in r.items() if k}
            content = d.get("content") or ""
            if not content:
                # 没有可识别列时，取最长的一列
                vals = [str(v) for v in r.values() if v]
                content = max(vals, key=len) if vals else ""
            if not content:
                continue
            rows.append({
                "comment_id": f"imp_{i}",
                "user_name": str(d.get("user_name") or ""),
                "content": str(content),
                "like_count": int(re.sub(r"[^\d]", "", str(d.get("like_count") or "0")) or 0),
                "reply_count": int(re.sub(r"[^\d]", "", str(d.get("reply_count") or "0")) or 0),
                "published_at": parse_time_any(d.get("published_at")),
            })
    else:
        for i, line in enumerate(raw.splitlines()):
            t = clean_text(line).strip("|-*·• \t")
            if len(t) < 2:
                continue
            # 形如 "用户：内容" 或 "用户(123赞): 内容"
            m = re.match(r"^(.{1,20}?)[：:]\s*(.+)$", t)
            user, content = ("", t)
            if m and len(m.group(2)) >= 4:
                user, content = m.group(1).strip(), m.group(2).strip()
            rows.append({"comment_id": f"imp_{i}", "user_name": user, "content": content,
                         "like_count": 0, "reply_count": 0, "published_at": None})
    return [r for r in rows if r.get("content")]
