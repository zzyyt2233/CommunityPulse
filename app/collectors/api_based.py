"""依赖公开接口的采集器：B站 / Steam / Reddit / 微博 / YouTube。"""
from __future__ import annotations

import re
import time
from typing import Callable
from urllib.parse import quote

from ..core.http_client import get_json, get_text, post_json
from ..core.credentials import cookie_for
from .base import (
    BEST_EFFORT,
    MANUAL,
    STABLE,
    BaseCollector,
    CollectResult,
    Comment,
    clean_text,
    dedup,
    register,
)
from .weibo_visitor import get_cookie


@register
class BilibiliCollector(BaseCollector):
    platform = "bilibili"
    label = "哔哩哔哩"
    reliability = STABLE
    patterns = (r"bilibili\.com/video/(BV[0-9A-Za-z]+)", r"bilibili\.com/video/av(\d+)",
                r"b23\.tv/(?:video/)?(BV[0-9A-Za-z]+)", r"bilibili\.com/opus/(\d+)",
                r"(?:www\.)?bilibili\.com/bangumi/play/(?:ss|ep)(\d+)")
    hint = "支持视频 BV/av 号、动态 opus 链接；按热度排序抓取主评论+楼中楼。"

    # B站评论接口单页条数：实测 ps>20 会返回 -400（ps out of bounds）
    PS = 20

    def _crawl_wbi(self, oid, rtype, mode, limit, headers, progress, res, base_url=""):
        """走 WBI 签名接口翻页抓取。返回 (停止原因, 评论总数)。

        这是匿名状态下唯一能大量翻页的方式：老接口 `/x/v2/reply` 匿名只能拿 3 条主评论，
        第 2 页起静默返回空；WBI 接口一页 20 条且可连续翻页。
        """
        from .bili_wbi import fetch_page, next_offset, total_count

        offset, page, total = "", 0, 0
        while len(res.comments) < limit and page < 200:
            payload = fetch_page(oid, rtype, mode, offset, headers)
            page += 1
            total = total or total_count(payload)
            if page == 1:
                for r in (payload.get("top_replies") or []):  # 置顶评论
                    res.comments.append(self._mk(r, base_url, pinned=True))
            replies = payload.get("replies") or []
            for r in replies:
                res.comments.append(self._mk(r, base_url))
                for sub in (r.get("replies") or []):
                    res.comments.append(self._mk(sub, base_url))
            self._tick(progress, min(len(res.comments), limit), f"B站 第{page}页")
            nxt, is_end = next_offset(payload)
            if is_end or not nxt or nxt == offset:
                return ("已抓到评论末尾（该视频全部评论已取完）" if is_end
                        else "服务端未提供下一页偏移量，提前结束"), total
            offset = nxt
        return ("" if len(res.comments) >= limit else "达到最大翻页上限（200 页）"), total

    def _crawl(self, oid, rtype, sort_flag, limit, headers, progress, res, base_url=""):
        """按一种排序翻页抓取，返回停止原因（空字符串表示抓满或未到底）。"""
        page = 1
        while len(res.comments) < limit:
            data = get_json(
                f"https://api.bilibili.com/x/v2/reply?type={rtype}&oid={oid}&sort={sort_flag}"
                f"&pn={page}&ps={self.PS}&nohot=0", headers=headers)
            code = data.get("code")
            if code not in (0, 12009):
                if code == -412:
                    return "B站触发风控（code -412）：请求太快或未登录，请在设置中填写 bilibili_cookie 并降低条数"
                if code == 12002:
                    return "评论区已关闭或不存在"
                return f"B站接口返回 code {code}（{data.get('message')}）"
            payload = data.get("data") or {}
            replies = payload.get("replies") or []
            if page == 1:
                for r in (payload.get("top_replies") or []):  # 置顶评论
                    res.comments.append(self._mk(r, base_url, pinned=True))
            if not replies:
                return "已到评论末尾（没有更多评论了）"
            for r in replies:
                res.comments.append(self._mk(r, base_url))
                for sub in (r.get("replies") or []):
                    res.comments.append(self._mk(sub, base_url))
            self._tick(progress, min(len(res.comments), limit), f"B站 第{page}页")
            page += 1
            if page > 200:
                return "达到最大翻页上限（200 页）"
        return ""

    @staticmethod
    def _mk(r, base_url="", pinned=False):
        return Comment(
            comment_id=str(r.get("rpid", "")),
            parent_id=str(r.get("root") or "") if not pinned else "",
            user_name=(r.get("member") or {}).get("uname", ""),
            content=clean_text((r.get("content") or {}).get("message", "")),
            like_count=int(r.get("like", 0)),
            reply_count=int(r.get("rcount", 0)),
            published_at=r.get("ctime") or None,
            url=base_url,
            extra={"level": (r.get("member") or {}).get("level_info", {}).get("current_level"),
                   "pinned": pinned},
        )

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        settings = settings or {}
        bv = re.search(r"(BV[0-9A-Za-z]+)", url)
        av = re.search(r"/av(\d+)", url)
        opus = re.search(r"/opus/(\d+)", url)
        oid, rtype, title = "", 1, ""
        if bv:
            info = get_json(f"https://api.bilibili.com/x/web-interface/view?bvid={bv.group(1)}")
            if info.get("code") != 0:
                raise RuntimeError(f"B站接口返回 {info.get('code')}: {info.get('message')}（可能触发风控，稍后重试或填 Cookie）")
            oid = str(info["data"]["aid"])
            title = info["data"].get("title", "")
        elif av:
            oid = av.group(1)
        elif opus:
            oid, rtype = opus.group(1), 17
        else:
            raise RuntimeError("无法从链接中识别 B站 稿件 ID")

        headers = {}
        if cookie_for(settings, "bilibili"):
            headers["Cookie"] = settings["bilibili_cookie"]
        else:
            headers["Cookie"] = ""  # 未登录也能拿热评，但翻页更容易被风控
        base = f"https://www.bilibili.com/video/{bv.group(1)}" if bv else url

        res = CollectResult(self.platform, oid, title, url, reliability=self.reliability,
                            requested=limit)
        # 先走 WBI 签名接口（匿名也能大量翻页），失败再降级到老接口。
        # mode: 3=热门（按点赞），2=最新（按时间）
        modes = (2,) if sort == "new" else ((3, 2) if sort in ("hot", "both") else (3,))
        reason = ""
        wbi_ok = False
        for mode in modes:
            try:
                r_text, total = self._crawl_wbi(oid, rtype, mode, limit, headers, progress, res, base)
                wbi_ok = True
                if total:
                    res.available = total
                if r_text:
                    reason = r_text
                if len(res.comments) >= limit or r_text:
                    break
            except Exception as exc:  # noqa: BLE001
                reason = f"WBI 接口失败：{exc}"
                break
        if not wbi_ok:
            # 降级：老 /x/v2/reply 接口（匿名只能拿少量热评，但至少不会一无所获）
            if sort == "new":
                legacy = self._crawl(oid, rtype, 1, limit, headers, progress, res, base)
            else:
                legacy = self._crawl(oid, rtype, 2, limit, headers, progress, res, base)
                if len(res.comments) < limit and "风控" not in legacy:
                    extra = self._crawl(oid, rtype, 1, limit, headers, progress, res, base)
                    legacy = extra or legacy
            reason = (reason + "；已降级到老接口：" + legacy) if reason else legacy
        res.stop_reason = reason

        res.comments = dedup(res.comments)
        # 取前 N 条时的排序口径要跟 sort 一致：
        # hot/both → 按热度（点赞）降序；new → 按时间从新到旧
        if sort == "new":
            res.comments.sort(key=lambda c: (-(c.published_at or 0), -c.like_count))
        else:
            res.comments.sort(key=lambda c: (-c.like_count, -(c.published_at or 0)))
        if len(res.comments) > limit:
            res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        has_cookie = bool(cookie_for(settings, "bilibili"))
        got = len(res.comments)
        if not res.complete:
            if not has_cookie:
                tip = ("仍未抓满。填 bilibili_cookie 可拿到更完整的评论区和楼中楼"
                       "（浏览器登录 B站 → F12 → Network 任意请求的 Cookie 请求头）")
                if res.available:
                    res.stop_reason = (
                        f"该视频共 {res.available} 条评论，本次取到 {got} 条。{tip}"
                        if res.stop_reason and "末尾" not in res.stop_reason
                        else (res.stop_reason or tip))
                    if "末尾" in res.stop_reason:
                        res.stop_reason = (f"已取完该视频全部可抓取评论 {got} 条"
                                           f"（平台显示 {res.available} 条，差额为楼中楼需登录才展开）。{tip}")
                else:
                    res.stop_reason = tip
            elif not res.stop_reason:
                res.stop_reason = "评论总量不足目标条数"
        elif res.available:
            res.stop_reason = f"已按目标取满 {got} 条（该视频共 {res.available} 条）"
        res.capacity_hint = ("已按点赞数从高到低排序，取前 N 条" if sort != "new"
                             else "已按时间从新到旧排序")
        return res


@register
class SteamCollector(BaseCollector):
    platform = "steam"
    label = "Steam 评测"
    reliability = STABLE
    patterns = (r"store\.steampowered\.com/app/(\d+)", r"steamcommunity\.com/app/(\d+)")
    hint = "填写游戏 AppID 或商店链接；官方接口，可拉取全部语言评测。"

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        settings = settings or {}
        m = self.extract_id(url) or re.search(r"app/(\d+)", url).group(1)
        lang = settings.get("steam_language", "all")
        day_range = settings.get("steam_day_range", "365")
        filt = "recent" if sort == "new" else "all"
        res = CollectResult(self.platform, m, f"Steam App {m}", url, reliability=self.reliability,
                            requested=limit)
        cursor = "*"
        page = 0
        while len(res.comments) < limit:
            # cursor 里含 + / 等字符，必须 URL 编码，否则下一批返回 success=8（空结果）
            cur = quote(str(cursor), safe="")
            data = get_json(
                f"https://store.steampowered.com/appreviews/{m}?json=1&filter={filt}&language={lang}"
                f"&day_range={day_range}&num_per_page=100&review_type=all&purchase_type=all"
                f"&cursor={cur}")
            if not data.get("success"):
                res.stop_reason = "Steam 接口未返回成功状态（可能已达末尾或 AppID 有误）"
                break
            reviews = data.get("reviews") or []
            if not reviews:
                break
            for rv in reviews:
                res.comments.append(Comment(
                    comment_id=str(rv.get("recommendationid", "")),
                    user_name=(rv.get("author") or {}).get("steamid", ""),
                    content=clean_text(rv.get("review", "")),
                    like_count=int(rv.get("votes_up", 0)),
                    reply_count=int(rv.get("comment_count", 0)),
                    published_at=rv.get("timestamp_created"),
                    url=f"https://steamcommunity.com/profiles/{(rv.get('author') or {}).get('steamid','')}/recommended/{m}/",
                    extra={
                        "recommend": bool(rv.get("voted_up")),
                        "playtime_hours": round((rv.get("author", {}).get("playtime_at_review") or 0) / 60, 1),
                        "language": rv.get("language"),
                    },
                ))
            page += 1
            self._tick(progress, len(res.comments), f"Steam 第{page}批")
            cursor = data.get("cursor")
            if not cursor or cursor == "*":
                res.stop_reason = res.stop_reason or "已到评测末尾"
                break
        res.comments = dedup(res.comments)
        if sort != "new":
            res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        if not res.complete and not res.stop_reason:
            res.stop_reason = "评测总量不足目标条数"
        res.capacity_hint = "Steam 每条最多 100 条/批，按有用度排序" if sort != "new" else "按最近发布排序"
        return res


@register
class RedditCollector(BaseCollector):
    platform = "reddit"
    label = "Reddit"
    reliability = STABLE
    patterns = (r"reddit\.com/r/[\w\-]+/comments/([\w]+)",)
    hint = "帖子链接即可，自动展开评论树。"

    def _walk(self, node, out: list[Comment], depth=0):
        for ch in (node.get("data", {}).get("children") or []):
            if ch.get("kind") == "more":
                continue
            d = ch.get("data", {})
            body = clean_text(d.get("body") or "")
            if body and body not in ("[deleted]", "[removed]"):
                out.append(Comment(
                    comment_id=d.get("id", ""),
                    user_name=d.get("author", ""),
                    content=body,
                    like_count=int(d.get("ups") or 0),
                    published_at=d.get("created_utc"),
                    url="https://www.reddit.com" + (d.get("permalink") or ""),
                    extra={"depth": depth, "subreddit": d.get("subreddit")},
                ))
            replies = d.get("replies")
            if isinstance(replies, dict) and depth < 4:
                self._walk(replies, out, depth + 1)

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        url = url.split("?")[0]
        rsort = (settings or {}).get("reddit_sort") or ("new" if sort == "new" else "top")
        data = get_json(f"{url}.json?limit=100&sort={rsort}&depth=6")
        res = CollectResult(self.platform, self.extract_id(url), "", url, reliability=self.reliability,
                            requested=limit)
        if isinstance(data, list) and data:
            post = data[0]["data"]["children"][0]["data"]
            res.source_title = post.get("title", "")
            res.source_id = post.get("id", "")
            for chunk in data[1:]:
                self._walk(chunk, res.comments)
        self._tick(progress, len(res.comments), "Reddit 完成")
        res.comments = dedup(res.comments)
        res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        if not res.complete:
            res.stop_reason = "该帖子评论总量不足目标条数（Reddit 单次接口最多返回该帖全部评论）"
        res.capacity_hint = "Reddit 返回整棵评论树，已按得票排序"
        return res


@register
class WeiboCollector(BaseCollector):
    platform = "weibo"
    label = "微博"
    reliability = BEST_EFFORT
    patterns = (r"weibo\.com/\d+/([A-Za-z0-9]+)", r"weibo\.com/detail/([A-Za-z0-9]+)",
                r"m\.weibo\.cn/(?:status|detail)/([A-Za-z0-9]+)")
    hint = "单条微博链接（含 mid）；走移动端热评流，可能需要 Cookie 才能翻更多页。"

    @staticmethod
    def _parse_time(s: str) -> float | None:
        try:
            return time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %z %Y"))
        except Exception:
            try:
                return time.mktime(time.strptime(s[:24], "%a %b %d %H:%M:%S %Y"))
            except Exception:
                return None

    def _fetch_ajax(self, api: str, cookie: str) -> dict:
        """带登录态请求微博 PC 端 ajax 接口。"""
        return get_json(api, headers={
            "Referer": "https://weibo.com/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
            "Cookie": cookie,
        })

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        settings = settings or {}
        mid = self.extract_id(url)
        if not mid:
            raise RuntimeError("无法识别微博 ID，请使用单条微博链接（如 weibo.com/xxx/AbCdEf123）")

        res = CollectResult(self.platform, mid, "", url, reliability=self.reliability,
                            requested=limit)
        cookie, how = get_cookie(cookie_for(settings, "weibo"))
        if not cookie:
            res.stop_reason = ("微博需要登录态：自动签发访客身份失败。"
                               "请在『设置』里填 weibo_cookie（浏览器登录微博后 F12 复制 Cookie 请求头）")
            res.complete = False
            return res

        # 先拿微博正文，便于任务标题识别
        try:
            info = self._fetch_ajax(f"https://weibo.com/ajax/statuses/show?id={mid}", cookie)
            text = ((info.get("data") or {}).get("text_raw") or "").strip()
            if text:
                res.source_title = text[:60]
        except Exception:
            pass

        # flow: 0=热门 1=最新
        flow = 1 if sort == "new" else 0
        max_id = ""
        page = 0
        total: int | None = None
        while len(res.comments) < limit and page < 60:
            api = (f"https://weibo.com/ajax/statuses/buildComments?flow={flow}&id={mid}"
                   f"&is_reload=1&is_show_bulletin=2&count=20&type=feed")
            if max_id:
                api += f"&max_id={max_id}"
            try:
                data = self._fetch_ajax(api, cookie)
            except Exception as exc:
                res.stop_reason = f"微博接口请求失败：{exc}"
                break
            rows = data.get("data") or []
            if not isinstance(rows, list) or not rows:
                break
            if total is None:
                total = data.get("total_number")
            for d in rows:
                if not isinstance(d, dict):
                    continue
                user = d.get("user") or {}
                src = (d.get("source") or "").replace("来自", "").strip()
                res.comments.append(Comment(
                    comment_id=str(d.get("idstr") or d.get("id") or ""),
                    user_name=user.get("screen_name", ""),
                    content=clean_text(d.get("text_raw") or d.get("text") or ""),
                    like_count=int(d.get("like_counts") or d.get("like_count") or 0),
                    reply_count=int(d.get("total_number") or 0),
                    published_at=self._parse_time(d.get("created_at") or ""),
                    url=f"https://weibo.com/{user.get('id', '')}/{d.get('bid') or mid}",
                    extra={
                        "verified": user.get("verified", False),
                        "level": user.get("mbtype") or 0,
                        "region": src,
                        "gender": user.get("gender", ""),
                        "avatar": user.get("profile_image_url", ""),
                        "user_url": f"https://weibo.com/u/{user.get('id', '')}",
                    },
                ))
            page += 1
            self._tick(progress, len(res.comments), f"微博 第{page}页")
            nxt = data.get("max_id")
            if not nxt or str(nxt) == str(max_id):
                break
            max_id = nxt

        res.comments = dedup(res.comments)
        if sort == "new":
            res.comments.sort(key=lambda c: -(c.published_at or 0))
        else:
            res.comments.sort(key=lambda c: (-c.like_count, -(c.published_at or 0)))
        if len(res.comments) > limit:
            res.comments = res.comments[:limit]

        if total:
            res.available = int(total)
        res.complete = len(res.comments) >= limit
        if not res.complete:
            is_guest = not cookie_for(settings, "weibo")
            if is_guest:
                res.stop_reason = (f"微博访客身份只能看到约 15 条评论（该微博共 {total or '?'} 条）。"
                                   "解决办法：在『设置』里填 weibo_cookie（浏览器登录微博 → F12 → "
                                   "Network 复制任意请求的 Cookie 请求头），即可翻页抓完整评论区")
                res.capacity_hint = f"{how}；{res.capacity_hint or ''}".strip("；")
            else:
                res.stop_reason = res.stop_reason or (
                    f"已抓到评论末尾或触发微博限流（该微博共 {total or '?'} 条）")
        return res


@register
class YouTubeCollector(BaseCollector):
    platform = "youtube"
    label = "YouTube"
    reliability = STABLE
    patterns = (r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w\-]{6,})",)
    hint = "需要 Google API Key（在设置中填写），否则只能抓到极少量页面文本。"

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        settings = settings or {}
        key = settings.get("youtube_api_key", "")
        vid = self.extract_id(url)
        if not key:
            raise RuntimeError("YouTube 需要 API Key，请在「设置」中填写 youtube_api_key（或改用手动导入）")
        res = CollectResult(self.platform, vid, "", url, reliability=self.reliability,
                            requested=limit)
        order = "time" if sort == "new" else "relevance"
        token = ""
        page = 0
        while len(res.comments) < limit:
            api = ("https://www.googleapis.com/youtube/v3/commentThreads?part=snippet,replies"
                   f"&videoId={vid}&key={key}&maxResults=100&order={order}&textFormat=plainText")
            if token:
                api += f"&pageToken={token}"
            data = get_json(api)
            if "error" in data:
                raise RuntimeError(f"YouTube 接口错误: {data['error'].get('message')}")
            for it in data.get("items", []):
                s = it["snippet"]["topLevelComment"]["snippet"]
                res.comments.append(Comment(
                    comment_id=it["id"],
                    user_name=s.get("authorDisplayName", ""),
                    content=clean_text(s.get("textDisplay", "")),
                    like_count=int(s.get("likeCount", 0)),
                    reply_count=int(it["snippet"].get("totalReplyCount", 0)),
                    published_at=_iso(s.get("publishedAt")),
                ))
                for rp in (it.get("replies", {}).get("comments") or []):
                    rs = rp["snippet"]
                    res.comments.append(Comment(
                        comment_id=rp["id"], parent_id=it["id"],
                        user_name=rs.get("authorDisplayName", ""),
                        content=clean_text(rs.get("textDisplay", "")),
                        like_count=int(rs.get("likeCount", 0)),
                        published_at=_iso(rs.get("publishedAt")),
                    ))
            page += 1
            self._tick(progress, len(res.comments), f"YouTube 第{page}页")
            token = data.get("nextPageToken")
            if not token:
                res.stop_reason = "已到评论末尾"
                break
        res.comments = dedup(res.comments)
        if sort != "new":
            res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        if not res.complete and not res.stop_reason:
            res.stop_reason = "评论总量不足目标条数"
        res.capacity_hint = "YouTube 按相关度/时间返回，已按点赞排序"
        return res


def _iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None


@register
class BilibiliDynamicCollector(BaseCollector):
    """B站动态（t.bilibili.com）走 opus 类型，独立识别。"""
    platform = "bilibili_dynamic"
    label = "B站动态"
    reliability = STABLE
    patterns = (r"t\.bilibili\.com/(\d+)",)
    hint = "动态链接，评论 type=17。"

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        did = self.extract_id(url)
        return BilibiliCollector().collect(f"https://www.bilibili.com/opus/{did}",
                                          limit=limit, progress=progress, settings=settings, sort=sort)


@register
class DouyinCollector(BaseCollector):
    platform = "douyin"
    label = "抖音"
    reliability = MANUAL
    patterns = (r"douyin\.com/video/(\d+)", r"iesdouyin\.com", r"douyin\.com/note/(\d+)")
    hint = ("抖音评论接口有签名风控（a_bogus），服务端直连不可靠。"
            "推荐用「浏览器采集助手」：web\\snippets\\browser_capture.js，在浏览器控制台粘贴运行即可导出 JSON。")

    # 抖音 web 评论接口（需要登录 Cookie，服务端直连仍可能被 a_bogus 风控）
    API = ("https://www.douyin.com/aweme/v1/web/comment/list/"
           "?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id={aweme}"
           "&cursor={cursor}&count=20&item_type=0&pc_client_type=1&version_code=170400"
           "&version_name=17.4.0&cookie_enabled=true&platform=PC&downlink=10")

    def _browser_helper_hint(self) -> str:
        return ("抖音请用「浏览器采集助手」：打开视频页 → F12 → Console 粘贴 "
                "web\\snippets\\browser_capture.js 内容 → 回车 → 自动滚动抓完 → "
                "脚本会自动回传到本工具并入库。")

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        aweme = self.extract_id(url)
        res = CollectResult(self.platform, aweme, "", url, reliability=self.reliability,
                            requested=limit, complete=False)
        if not aweme:
            res.stop_reason = "无法识别抖音视频 ID，请使用 https://www.douyin.com/video/xxxx 形式的链接"
            return res
        cookie = cookie_for(settings, "douyin")
        if not cookie:
            res.stop_reason = ("抖音需要登录态：请在『平台接入』里点「打开登录页」→ 登录后点「一键获取」，"
                               "或直接用浏览器采集助手")
            res.warnings.append(self._browser_helper_hint())
            return res

        headers = {
            "Cookie": cookie,
            "Referer": f"https://www.douyin.com/video/{aweme}",
            "Accept": "application/json, text/plain, */*",
        }
        cursor = 0
        page = 0
        while len(res.comments) < limit and page < 60:
            try:
                data = get_json(self.API.format(aweme=aweme, cursor=cursor), headers=headers)
            except Exception as exc:  # noqa: BLE001
                res.stop_reason = f"抖音接口请求失败（{exc}）"
                res.warnings.append(self._browser_helper_hint())
                break
            if not isinstance(data, dict) or str(data.get("status_code")) not in ("0", "None"):
                res.stop_reason = (f"抖音返回风控状态 status_code={data.get('status_code')}"
                                   "（a_bogus 签名校验，服务端直连无法绕过）")
                res.warnings.append(self._browser_helper_hint())
                break
            rows = data.get("comments") or []
            if not rows:
                res.stop_reason = "已到评论末尾"
                break
            for d in rows:
                user = d.get("user") or {}
                res.comments.append(Comment(
                    comment_id=str(d.get("cid") or ""),
                    user_name=user.get("nickname", ""),
                    content=clean_text(d.get("text") or ""),
                    like_count=int(d.get("digg_count") or 0),
                    reply_count=int(d.get("reply_comment_total") or 0),
                    published_at=d.get("create_time") or None,
                    url=url,
                    extra={"region": d.get("ip_label", ""),
                           "user_url": f"https://www.douyin.com/user/{user.get('sec_uid','')}"},
                ))
            page += 1
            self._tick(progress, len(res.comments), f"抖音 第{page}页")
            if data.get("has_more") != 1:
                res.stop_reason = "已到评论末尾"
                break
            cursor = int(data.get("cursor") or 0) + 20

        res.comments = dedup(res.comments)
        if sort != "new":
            res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.available = int(data.get("total_number") or 0) if isinstance(data, dict) else 0
        res.complete = len(res.comments) >= limit
        if not res.comments:
            res.stop_reason = res.stop_reason or "抖音未返回评论（多为签名风控），请用浏览器采集助手"
            res.warnings.append(self._browser_helper_hint())
        return res


@register
class XiaohongshuCollector(BaseCollector):
    platform = "xiaohongshu"
    label = "小红书"
    reliability = MANUAL
    patterns = (r"xiaohongshu\.com/(?:explore|discovery/item)/([a-z0-9]+)", r"xhslink\.com")
    hint = ("小红书需登录态 + 签名，直连不可靠。"
            "推荐用「浏览器采集助手」：web\\snippets\\browser_capture.js。")

    # 小红书评论接口（edith），需要登录 Cookie；缺 x-s 签名时通常直接失败
    API = "https://edith.xiaohongshu.com/api/sns/web/v2/comment/page"

    def _browser_helper_hint(self) -> str:
        return ("小红书请用「浏览器采集助手」：打开笔记页 → F12 → Console 粘贴 "
                "web\\snippets\\browser_capture.js 内容 → 回车 → 自动滚动抓完 → "
                "脚本会自动回传到本工具并入库。")

    def collect(self, url, limit=500, progress=None, settings=None, sort="hot"):
        note_id = self.extract_id(url)
        res = CollectResult(self.platform, note_id, "", url, reliability=self.reliability,
                            requested=limit, complete=False)
        if not note_id:
            res.stop_reason = "无法识别小红书笔记 ID（xhslink 短链需先在浏览器打开后复制真实链接）"
            res.warnings.append(self._browser_helper_hint())
            return res
        cookie = cookie_for(settings, "xiaohongshu")
        if not cookie:
            res.stop_reason = ("小红书需要登录态：请在『平台接入』里点「打开登录页」→ 登录后点「一键获取」，"
                               "或直接用浏览器采集助手")
            res.warnings.append(self._browser_helper_hint())
            return res

        headers = {"Cookie": cookie, "Referer": url, "Origin": "https://www.xiaohongshu.com",
                   "Accept": "application/json, text/plain, */*"}
        cursor = ""
        page = 0
        data: dict = {}
        while len(res.comments) < limit and page < 50:
            payload = {"note_id": note_id, "cursor": cursor, "top_comment_id": "",
                       "image_formats": ["jpg", "webp", "avif"]}
            try:
                data = post_json(self.API, payload, headers=headers)
            except Exception as exc:  # noqa: BLE001
                res.stop_reason = f"小红书接口请求失败（{exc}）"
                res.warnings.append(self._browser_helper_hint())
                break
            if not isinstance(data, dict) or not data.get("success"):
                res.stop_reason = (f"小红书拒绝了请求（{data.get('msg') or '缺少 x-s 签名'}），"
                                   "服务端直连通常不可用")
                res.warnings.append(self._browser_helper_hint())
                break
            for d in ((data.get("data") or {}).get("comments") or []):
                user = d.get("user_info") or {}
                res.comments.append(Comment(
                    comment_id=str(d.get("id") or ""),
                    user_name=user.get("nickname", ""),
                    content=clean_text(d.get("content") or ""),
                    like_count=int(d.get("like_count") or 0),
                    reply_count=int(d.get("sub_comment_count") or 0),
                    published_at=(d.get("create_time") or 0) / 1000 or None,
                    url=url,
                    extra={"region": d.get("ip_location", ""),
                           "user_url": f"https://www.xiaohongshu.com/user/profile/{user.get('user_id','')}"},
                ))
            page += 1
            self._tick(progress, len(res.comments), f"小红书 第{page}页")
            if not (data.get("data") or {}).get("has_more"):
                res.stop_reason = "已到评论末尾"
                break
            cursor = (data.get("data") or {}).get("cursor") or ""

        res.comments = dedup(res.comments)
        if sort != "new":
            res.comments.sort(key=lambda c: -c.like_count)
        res.comments = res.comments[:limit]
        res.complete = len(res.comments) >= limit
        if not res.comments:
            res.stop_reason = res.stop_reason or "小红书未返回评论（需 x-s 签名），请用浏览器采集助手"
            res.warnings.append(self._browser_helper_hint())
        return res
