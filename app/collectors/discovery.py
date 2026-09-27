"""来源发现：按关键词找到值得监测的帖子/视频。

借鉴 MediaCrawler 的 search 模式 —— 做舆情不能只盯已知链接，得能主动发现
「大家在讨论什么」。

分两类平台：
  - 服务端直连搜索（bilibili / reddit / steam / weibo / tieba / nga）：
    用各平台公开/半公开接口，登录平台复用已保存的 Cookie 提升成功率。
  - 浏览器助手搜索（douyin / xiaohongshu / taptap）：搜索也过风控/接口失效，
    必须在已登录浏览器里跑，search() 不负责，由 server 直接路由到
    browser_cdp.search_via_browser。
"""

from __future__ import annotations

import re
import time
import urllib.parse

from ..core.http_client import get_json, get_text
from ..core.credentials import cookie_for
from .bili_wbi import sign

# order: totalrank=综合 click=播放 pubdate=最新 dm=弹幕 stow=收藏
_BILI_ORDER = {
    "hot": "click",
    "new": "pubdate",
    "danmaku": "dm",
    "favorite": "stow",
    "default": "totalrank",
}


def _clean(s: str) -> str:
    return re.sub(r"</?em[^>]*>", "", s or "").strip()


def _bili_once(keyword: str, order: str, limit: int, page: int = 1) -> list[dict]:
    """单页 B站搜索。"""
    params = {
        "search_type": "video",
        "keyword": keyword,
        "page": str(page),
        "order": order,
        "platform": "web",
        "web_location": "1430654",
    }
    try:
        data = get_json(
            "https://api.bilibili.com/x/web-interface/wbi/search/type?" + sign(params))
    except Exception:
        return []
    if data.get("code") != 0:
        return []
    return ((data.get("data") or {}).get("result") or [])


def _bili_row(r: dict) -> dict | None:
    bvid = r.get("bvid") or ""
    if not bvid:
        return None
    return {
        "platform": "bilibili",
        "id": bvid,
        "title": _clean(r.get("title", "")),
        "author": r.get("author", ""),
        "url": f"https://www.bilibili.com/video/{bvid}",
        "play": int(r.get("play") or 0),
        "danmaku": int(r.get("video_review") or r.get("dm") or 0),
        "reply": int(r.get("review") or 0),
        "like": int(r.get("like") or 0),
        "favorite": int(r.get("favorites") or 0),
        "published_at": r.get("pubdate") or None,
        "duration": r.get("duration", ""),
        "cover": ("https:" + r["pic"]) if (r.get("pic") or "").startswith("//") else r.get("pic", ""),
    }


def search_bilibili(keyword: str, limit: int = 20, sort: str = "hot",
                    settings: dict | None = None) -> list[dict]:
    """B站视频搜索（wbi 签名接口，匿名可用）。

    B站搜索是多词 AND，像「原神 卡顿」这种组合常常 0 结果，
    所以拿不到结果时自动退到第一个词再试一次。
    """
    order = _BILI_ORDER.get(sort, "click")
    out: list[dict] = []
    got = 0
    p = 1
    kw = keyword.strip()
    tried_fallback = False
    while got < limit and p <= 5:
        rows = _bili_once(kw, order, limit, p)
        if not rows and not tried_fallback and len(kw.split()) > 1:
            # 多词太严格，退回主关键词
            tried_fallback = True
            kw = kw.split()[0]
            p = 1
            rows = _bili_once(kw, order, limit, p)
        if not rows:
            break
        params = {
            "search_type": "video",
            "keyword": keyword,
            "page": str(p),
            "order": order,
            "platform": "web",
            "web_location": "1430654",
        }
        try:
            data = get_json(
                "https://api.bilibili.com/x/web-interface/wbi/search/type?"
                + sign(params))
        except Exception:
            # 签名接口偶发失败时退回无签名版本
            try:
                data = get_json(
                    "https://api.bilibili.com/x/web-interface/search/type?"
                    + urllib.parse.urlencode(params))
            except Exception:
                break
        if data.get("code") not in (0,):
            break
        rows = ((data.get("data") or {}).get("result") or [])
        if not rows:
            break
        for r in rows:
            row = _bili_row(r)
            if row:
                out.append(row)
                got += 1
                if got >= limit:
                    break
        if len(rows) < 20:
            break
        p += 1
        time.sleep(0.3)
    return out


def search_steam(keyword: str, limit: int = 20, sort: str = "hot",
                 settings: dict | None = None) -> list[dict]:
    """Steam 游戏搜索（按名称找 appid，用于评测采集）。"""
    try:
        data = get_json("https://store.steampowered.com/api/storesearch/"
                        f"?term={urllib.parse.quote(keyword)}&l=schinese&cc=CN")
    except Exception:
        return []
    out = []
    for r in (data.get("items") or [])[:limit]:
        appid = r.get("id")
        if not appid:
            continue
        out.append({
            "platform": "steam",
            "id": str(appid),
            "title": r.get("name", ""),
            "author": "",
            "url": f"https://store.steampowered.com/app/{appid}/",
            "play": 0, "danmaku": 0, "reply": 0, "like": 0, "favorite": 0,
            "published_at": None, "duration": "", "cover": r.get("tiny_image", ""),
        })
    return out


def search_reddit(keyword: str, limit: int = 20, sort: str = "hot",
                  settings: dict | None = None) -> list[dict]:
    """Reddit 全站搜索（公开 JSON，匿名可用）。"""
    try:
        data = get_json("https://www.reddit.com/search.json?"
                        + urllib.parse.urlencode({"q": keyword, "limit": limit,
                                                  "sort": sort if sort in ("hot", "new", "top", "relevance")
                                                  else "relevance"}))
    except Exception:
        return []
    out = []
    for c in ((data.get("data") or {}).get("children") or []):
        d = c.get("data") or {}
        perm = d.get("permalink") or ""
        if not perm:
            continue
        out.append({
            "platform": "reddit",
            "id": d.get("id", ""),
            "title": d.get("title", ""),
            "author": d.get("author", ""),
            "url": "https://www.reddit.com" + perm,
            "play": 0, "danmaku": 0,
            "reply": int(d.get("num_comments") or 0),
            "like": int(d.get("score") or 0),
            "favorite": 0,
            "published_at": int(d.get("created_utc") or 0) or None,
            "duration": "", "cover": "",
        })
    return out


# ----------------------------------------------------------------------------
# 以下为「已登录平台」的搜索适配器：复用 cookie_for 取登录态，提升成功率
# ----------------------------------------------------------------------------

def search_weibo(keyword: str, limit: int = 20, sort: str = "hot",
                 settings: dict | None = None) -> list[dict]:
    """微博搜索（m.weibo.cn 容器接口，带登录 Cookie 更稳）。

    返回 m.weibo.cn/detail/{mid}，能被 WeiboCollector 直接识别。
    """
    cookie = cookie_for(settings, "weibo")
    headers = {
        "Referer": "https://m.weibo.cn/",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
        "MWeibo-Pwa": "1",
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
                       "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
    }
    if cookie:
        headers["Cookie"] = cookie
    containerid = "100103type%3D1%26q%3D" + urllib.parse.quote(keyword)
    url = ("https://m.weibo.cn/api/container/getIndex?"
           + urllib.parse.urlencode({"containerid": containerid,
                                     "page_type": "searchall", "page": 1}))
    try:
        data = get_json(url, headers=headers)
    except Exception:
        return []
    cards = ((data.get("data") or {}).get("cards") or [])
    out = []
    for card in cards:
        mblog = card.get("mblog") or {}
        mid = mblog.get("id") or mblog.get("mid")
        if not mid:
            continue
        text = _clean(mblog.get("text") or "")
        if not text:
            continue
        out.append({
            "platform": "weibo",
            "id": str(mid),
            "title": text,
            "author": (mblog.get("user") or {}).get("screen_name", ""),
            "url": f"https://m.weibo.cn/detail/{mid}",
            "play": 0, "danmaku": 0,
            "reply": int(mblog.get("comments_count") or 0),
            "like": int(mblog.get("attitudes_count") or 0),
            "favorite": int(mblog.get("reposts_count") or 0),
            "published_at": None, "duration": "", "cover": "",
        })
        if len(out) >= limit:
            break
    return out


def search_tieba(keyword: str, limit: int = 20, sort: str = "hot",
                 settings: dict | None = None) -> list[dict]:
    """贴吧全吧搜索（需百度 Cookie），返回相关帖子链接。"""
    cookie = cookie_for(settings, "tieba")
    headers = {"Accept-Language": "zh-CN,zh;q=0.9"}
    if cookie:
        headers["Cookie"] = cookie
    url = ("https://tieba.baidu.com/f/search/res?"
           + urllib.parse.urlencode({"qw": keyword, "rn": min(limit, 50), "pn": 0}))
    try:
        html = get_text(url, headers=headers)
    except Exception:
        return []
    # 优先抓带标题的结果：<a href="/p/123" ... title="...">标题</a>
    pairs = re.findall(r'href="/p/(\d+)"[^>]*title="([^"]*)"', html)
    if not pairs:
        # 回退：只抓 tid
        ids = re.findall(r'/p/(\d{6,})', html)
        pairs = [(t, "") for t in dict.fromkeys(ids)]
    seen: set[str] = set()
    out = []
    for tid, title in pairs:
        if tid in seen:
            continue
        seen.add(tid)
        out.append({
            "platform": "tieba",
            "id": tid,
            "title": _clean(title) or f"贴吧帖子 #{tid}",
            "author": "",
            "url": f"https://tieba.baidu.com/p/{tid}",
            "play": 0, "danmaku": 0, "reply": 0, "like": 0, "favorite": 0,
            "published_at": None, "duration": "", "cover": "",
        })
        if len(out) >= limit:
            break
    return out


def search_nga(keyword: str, limit: int = 20, sort: str = "hot",
               settings: dict | None = None) -> list[dict]:
    """NGA 全站搜贴（GBK 页面），返回相关帖子链接。"""
    cookie = cookie_for(settings, "nga")
    headers: dict = {}
    if cookie:
        headers["Cookie"] = cookie
    url = ("https://bbs.nga.cn/thread.php?"
           + urllib.parse.urlencode({"key": keyword, "t": 1, "page": 1}))
    try:
        raw = get_text(url, headers=headers)
    except Exception:
        return []
    pairs = re.findall(r'href="[^"]*?read\.php\?tid=(\d+)[^"]*"[^>]*>(.*?)</a>', raw, re.S)
    seen: set[str] = set()
    out = []
    for tid, title in pairs:
        if tid in seen:
            continue
        seen.add(tid)
        t = _clean(title)
        if not t or len(t) < 2:
            continue
        out.append({
            "platform": "nga",
            "id": tid,
            "title": t,
            "author": "",
            "url": f"https://bbs.nga.cn/read.php?tid={tid}",
            "play": 0, "danmaku": 0, "reply": 0, "like": 0, "favorite": 0,
            "published_at": None, "duration": "", "cover": "",
        })
        if len(out) >= limit:
            break
    return out


def search_taptap(keyword: str, limit: int = 20, sort: str = "hot",
                  settings: dict | None = None) -> list[dict]:
    """TapTap 搜索游戏/应用，返回 App 链接（再下钻到评测）。"""
    cookie = cookie_for(settings, "taptap")
    headers = {"Accept": "application/json", "Referer": "https://www.taptap.cn/"}
    if cookie:
        headers["Cookie"] = cookie
    url = ("https://www.taptap.cn/webapiv2/search?"
           + urllib.parse.urlencode({"kw": keyword, "type": "app",
                                     "limit": min(limit, 50)}))
    try:
        data = get_json(url, headers=headers)
    except Exception:
        return []
    rows = (data.get("data") or {}).get("list") or []
    out = []
    for r in rows:
        app_id = r.get("id") or (r.get("app") or {}).get("id")
        if not app_id:
            continue
        title = _clean(r.get("title") or (r.get("app") or {}).get("title") or "")
        author = ""
        a = r.get("author")
        if isinstance(a, dict):
            author = a.get("name", "")
        votes = int(r.get("votes") or r.get("rating") or 0)
        out.append({
            "platform": "taptap",
            "id": str(app_id),
            "title": title or f"TapTap 应用 #{app_id}",
            "author": author,
            "url": f"https://www.taptap.cn/app/{app_id}",
            "play": 0, "danmaku": 0,
            "reply": int(r.get("comment_count") or 0),
            "like": votes, "favorite": 0,
            "published_at": None, "duration": "", "cover": "",
        })
        if len(out) >= limit:
            break
    return out


# rank_key: 按该字段降序挑「热度前 N」；None 表示无热度指标，保持搜索顺序
# needs_login: 没有登录 Cookie 时 keyword-collect 直接提示先登录
# browser: True 表示搜索本身必须在已登录浏览器里跑（search() 不负责）
PLATFORMS = {
    "bilibili": {
        "label": "B站", "fn": search_bilibili,
        "sorts": ["hot", "new", "danmaku", "favorite"],
        "rank_key": "play", "needs_login": False, "browser": False,
        "reliability": "stable",
        "note": "匿名可用，返回播放/弹幕/评论数",
    },
    "reddit": {
        "label": "Reddit", "fn": search_reddit,
        "sorts": ["hot", "new", "top"],
        "rank_key": "like", "needs_login": False, "browser": False,
        "reliability": "best-effort",
        "note": "海外站点，国内直连可能超时",
    },
    "steam": {
        "label": "Steam", "fn": search_steam,
        "sorts": [],
        "rank_key": None, "needs_login": False, "browser": False,
        "reliability": "best-effort",
        "note": "搜索接口国内常不可达，建议直接粘贴游戏商店链接",
    },
    "weibo": {
        "label": "微博", "fn": search_weibo,
        "sorts": ["hot", "new"],
        "rank_key": "like", "needs_login": True, "browser": False,
        "reliability": "best-effort",
        "note": "需登录 Cookie（在『平台接入』登录微博后一键获取），否则结果受限",
    },
    "tieba": {
        "label": "百度贴吧", "fn": search_tieba,
        "sorts": ["hot", "new"],
        "rank_key": None, "needs_login": True, "browser": False,
        "reliability": "best-effort",
        "note": "需百度 Cookie；搜索返回相关帖子链接",
    },
    "nga": {
        "label": "NGA 论坛", "fn": search_nga,
        "sorts": ["hot", "new"],
        "rank_key": None, "needs_login": True, "browser": False,
        "reliability": "best-effort",
        "note": "搜索需登录态（NGA 要求账号有威望）；登录后由本工具带 Cookie 尝试",
    },
    "taptap": {
        "label": "TapTap", "fn": None,
        "sorts": ["hot", "new"],
        "rank_key": "like", "needs_login": True, "browser": True,
        "reliability": "best-effort",
        "note": "走浏览器助手：在已登录浏览器里搜索游戏/应用，并逐条抓 App 评测",
    },
    "douyin": {
        "label": "抖音", "fn": None,
        "sorts": ["hot"],
        "rank_key": None, "needs_login": True, "browser": True,
        "reliability": "best-effort",
        "note": "走浏览器助手：在已登录浏览器里搜索并逐条抓评论",
    },
    "xiaohongshu": {
        "label": "小红书", "fn": None,
        "sorts": ["hot"],
        "rank_key": None, "needs_login": True, "browser": True,
        "reliability": "best-effort",
        "note": "走浏览器助手：在已登录浏览器里搜索并逐条抓评论",
    },
}


def search(platform: str, keyword: str, limit: int = 20, sort: str = "hot",
           settings: dict | None = None) -> list[dict]:
    """按平台关键词搜索；浏览器类平台返回空（由 server 路由到浏览器助手）。"""
    cfg = PLATFORMS.get(platform)
    if not cfg or cfg.get("browser"):
        return []
    fn = cfg.get("fn")
    if not callable(fn):
        return []
    try:
        return fn(keyword, limit, sort, settings)
    except Exception:
        return []
