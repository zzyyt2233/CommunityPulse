"""网页结构解析型采集器：贴吧 / NGA / TapTap。

这些平台没有稳定公开接口，解析规则可能随改版失效；失败时会自动回退到
通用文本抽取（generic）。
"""
from __future__ import annotations

import re
from typing import Callable

from ..core.http_client import get_text
from ..core.credentials import cookie_for
from .base import (
    BEST_EFFORT,
    RESTRICTED,
    BaseCollector,
    CollectResult,
    Comment,
    clean_text,
    dedup,
    register,
)


@register
class TiebaCollector(BaseCollector):
    platform = "tieba"
    label = "百度贴吧"
    reliability = BEST_EFFORT
    patterns = (r"tieba\.baidu\.com/p/(\d+)", r"tiebac\.baidu\.com/p/(\d+)")
    hint = "贴吧帖子链接；解析 PC 端页面，楼层多时建议配合手动导入补充。"

    def collect(self, url, limit=500, progress=None, settings=None, sort='hot'):
        tid = self.extract_id(url)
        res = CollectResult(self.platform, tid, "", url, reliability=self.reliability,
                            requested=limit)
        cookie = cookie_for(settings, "tieba")
        headers = {"Accept-Language": "zh-CN,zh;q=0.9"}
        if cookie:
            headers["Cookie"] = cookie
        pn = 1
        while len(res.comments) < limit and pn <= 10:
            try:
                html = get_text(f"{url.split('?')[0]}?pn={pn}" if pn > 1 else url, headers=headers)
            except Exception as exc:  # noqa: BLE001
                res.warnings.append(f"贴吧第 {pn} 页请求失败：{exc}")
                break
            if not html:
                break
            if not res.source_title:
                m = re.search(r"<title>(.*?)</title>", html, re.S)
                if m:
                    res.source_title = clean_text(m.group(1))[:80]
            blocks = re.findall(
                r'<div[^>]*class="[^"]*d_post_content[^"]*"[^>]*>(.*?)</div>', html, re.S)
            authors = re.findall(r'class="p_author_name[^"]*"[^>]*>(.*?)</a>', html, re.S)
            if not blocks:
                res.warnings.append(f"第 {pn} 页未解析到楼层内容（可能需要登录或页面结构变化）")
                break
            for i, blk in enumerate(blocks):
                res.comments.append(Comment(
                    comment_id=f"{tid}_{pn}_{i}",
                    user_name=clean_text(authors[i]) if i < len(authors) else "",
                    content=clean_text(blk),
                    like_count=0,
                    published_at=None,
                    url=f"{url}#{pn}",
                ))
            self._tick(progress, len(res.comments), f"贴吧 第{pn}页")
            pn += 1
        res.comments = dedup(res.comments)[:limit]
        res.complete = len(res.comments) >= limit
        if not res.comments:
            res.warnings.append("建议使用手动导入：复制帖子内容粘贴进文本框。")
            res.stop_reason = ("页面未解析到楼层内容。贴吧现已强制登录：请在『平台接入』里点「打开登录页」"
                               "登录百度账号后点「一键获取」，再重新采集"
                               if not cookie else
                               "已带百度 Cookie 仍未解析到楼层，可能页面结构变化，请用手动导入")
        elif not res.complete:
            res.stop_reason = "楼层总量不足目标条数（贴吧单次最多解析 10 页）"
        res.capacity_hint = "贴吧按楼层顺序，无点赞数据"
        return res


@register
class NgaCollector(BaseCollector):
    platform = "nga"
    label = "NGA 论坛"
    reliability = BEST_EFFORT
    patterns = (r"bbs\.nga\.cn/read\.php\?tid=(\d+)", r"nga\.178\.com/read\.php\?tid=(\d+)",
                r"ngabbs\.com/read\.php\?tid=(\d+)")
    hint = "NGA 帖子链接；走 nuke.php 数据接口，页面为 GBK 编码。"

    def collect(self, url, limit=500, progress=None, settings=None, sort='hot'):
        tid = self.extract_id(url)
        res = CollectResult(self.platform, tid, "", url, reliability=self.reliability,
                            requested=limit)
        cookie = cookie_for(settings, "nga")
        headers = {"Referer": f"https://bbs.nga.cn/read.php?tid={tid}"}
        if cookie:
            headers["Cookie"] = cookie
        page = 1
        while len(res.comments) < limit and page <= 20:
            api = (f"https://bbs.nga.cn/nuke.php?__lib=post&__act=list&tid={tid}&page={page}"
                   f"&__output=8&__charset=gbk")
            try:
                raw = get_text(api, headers=headers)
            except Exception as e:  # noqa: BLE001
                res.warnings.append(f"NGA 第{page}页请求失败: {e}")
                break
            items = re.findall(r'"content":"(.*?)","', raw, re.S)
            authors = re.findall(r'"author":"(.*?)"', raw)
            dates = re.findall(r'"postdate":(\d+)', raw)
            if not items:
                # 退回 HTML 解析
                html = get_text(f"https://bbs.nga.cn/read.php?tid={tid}&page={page}")
                items = re.findall(r'id="postcontent\d+">(.*?)</p>', html, re.S)
                authors = re.findall(r'class="author"[^>]*>(.*?)</a>', html, re.S)
            if not items:
                res.warnings.append(f"第{page}页未解析到内容（NGA 需登录或结构变化）")
                break
            for i, c in enumerate(items):
                txt = clean_text(c.encode("latin-1", "ignore").decode("gbk", "ignore")
                                 if _looks_gbk(c) else c)
                if not txt:
                    continue
                res.comments.append(Comment(
                    comment_id=f"{tid}_{page}_{i}",
                    user_name=clean_text(authors[i]) if i < len(authors) else "",
                    content=txt,
                    published_at=float(dates[i]) if i < len(dates) and dates[i].isdigit() else None,
                    url=f"https://bbs.nga.cn/read.php?tid={tid}&page={page}",
                ))
            self._tick(progress, len(res.comments), f"NGA 第{page}页")
            page += 1
        res.comments = dedup(res.comments)[:limit]
        res.complete = len(res.comments) >= limit
        if not res.comments:
            res.warnings.append("NGA 抓取失败时，请用手动导入粘贴帖子内容。")
            res.stop_reason = "NGA 需登录或页面结构变化，未解析到内容"
        elif not res.complete:
            res.stop_reason = "帖子回复总量不足目标条数"
        res.capacity_hint = "NGA 按楼层顺序，无点赞数据"
        return res


def _looks_gbk(s: str) -> bool:
    """接口把 GBK 字节按 latin-1 读进来了，需要回转。"""
    return bool(s) and any("\u00c0" <= ch <= "\u00ff" or "\u0080" <= ch <= "\u00bf" for ch in s[:50])


@register
class TapTapCollector(BaseCollector):
    platform = "taptap"
    label = "TapTap"
    reliability = RESTRICTED
    patterns = (r"taptap\.(?:cn|com)/app/(\d+)", r"taptap\.(?:cn|com)/topic/(\d+)")
    hint = "TapTap 评论接口带签名校验，直连成功率有限；可在设置里粘贴 TapTap Cookie 提升成功率，失败请用手动导入。"

    def collect(self, url, limit=500, progress=None, settings=None, sort='hot'):
        settings = settings or {}
        app_id = self.extract_id(url)
        res = CollectResult(self.platform, app_id, "", url, reliability=self.reliability,
                            requested=limit)
        headers = {"Accept": "application/json",
                   "Referer": "https://www.taptap.cn/"}
        cookie = cookie_for(settings, "taptap")
        if cookie:
            headers["Cookie"] = cookie
        page = 0
        tsort = "new" if sort == "new" else "default"
        while len(res.comments) < limit and page < 50:
            api = ("https://www.taptap.cn/webapiv2/review/v2/list-by-app"
                   f"?app_id={app_id}&limit=20&sort={tsort}&offset={page * 20}")
            try:
                data = get_text(api, headers=headers)
                import json as _json
                payload = _json.loads(data)
            except Exception as e:  # noqa: BLE001
                res.warnings.append(f"TapTap 接口不可用（{e}），请用手动导入或填 Cookie")
                break
            rows = (payload.get("data") or {}).get("list") or []
            if not rows:
                break
            for r in rows:
                author = (r.get("author") or {})
                res.comments.append(Comment(
                    comment_id=str(r.get("id", "")),
                    user_name=author.get("name", ""),
                    content=clean_text(r.get("text") or r.get("content") or ""),
                    like_count=int(r.get("votes", {}).get("up", 0) if isinstance(r.get("votes"), dict) else r.get("like_count", 0) or 0),
                    reply_count=int(r.get("comments", {}).get("total", 0) if isinstance(r.get("comments"), dict) else 0),
                    published_at=r.get("created_time") if isinstance(r.get("created_time"), (int, float)) else None,
                    url=url,
                    extra={"score": (r.get("score") or {}).get("value") if isinstance(r.get("score"), dict) else r.get("score")},
                ))
            page += 1
            self._tick(progress, len(res.comments), f"TapTap 第{page}页")
        res.comments = dedup(res.comments)
        res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        if not res.comments:
            res.warnings.append("TapTap 直连失败，建议手动导入评论。")
            res.stop_reason = ("TapTap 接口带签名校验，未登录时通常直接失败：请在『平台接入』里点"
                               "「打开登录页」登录 TapTap 后点「一键获取」，再重新采集"
                               if not cookie else
                               "已带 TapTap Cookie 仍未取到评论，请改用手动导入或浏览器采集助手")
        elif not res.complete:
            res.stop_reason = "评论总量不足目标条数（TapTap 单次最多 50 页）"
        res.capacity_hint = "TapTap 每页 20 条，已按点赞排序"
        return res
