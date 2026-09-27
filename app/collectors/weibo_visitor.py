"""微博访客身份（无需账号）。

微博几乎所有数据接口都要求登录态，未登录会返回 ``ok:-100`` 并跳转 login.php，
移动端 m.weibo.cn 更是直接返回 HTTP 432。

但微博官方自己提供了「游客身份」签发流程（浏览器未登录访问 weibo.com 时走的就是它）：
    1. GET  /visitor/genvisitor  -> 拿到 tid
    2. GET  /visitor/visitor?a=incarnate&t=<tid> -> 下发 SUB / SUBP cookie

这是平台自身的公开流程，不需要账号、不破解任何签名。实测拿到访客 cookie 后
buildComments 可正常返回评论（游客上限约 15 条，填入真实账号 cookie 可突破）。
"""

from __future__ import annotations

import http.cookiejar
import json
import random
import re
import time
import urllib.parse
import urllib.request

from ..core.config import DEFAULT_HEADERS

PASSPORT = "https://passport.weibo.com"
_CACHE: dict[str, tuple[float, str]] = {}  # scope -> (过期时间, cookie串)
_TTL = 6 * 3600  # 访客 cookie 有效期约数小时，留足余量

_FP = {
    "os": "1",
    "browser": "Chrome126,Chrome,126",
    "fonts": "undefined",
    "screenInfo": "1920*1080*24",
    "platform": "Win32",
    "language": "zh-CN",
    "hardwareConcurrency": "8",
    "deviceMemory": "8",
    "timezone": "Asia/Shanghai",
    "webgl": "ANGLE",
    "webp": "true",
    "doNotTrack": "false",
    "touchSupport": "0",
}


def _get(url: str, referer: str, jar: http.cookiejar.CookieJar, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_HEADERS.get("User-Agent", ""),
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": referer,
        "Connection": "keep-alive",
    })
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener.open(req, timeout=timeout).read().decode("utf-8", "ignore")


def issue_visitor_cookie(timeout: int = 20) -> str:
    """签发一份微博访客 cookie（SUB / SUBP），失败返回空串。"""
    jar = http.cookiejar.CookieJar()
    fp = json.dumps(_FP, separators=(",", ":"))
    url = f"{PASSPORT}/visitor/genvisitor?cb=gen_callback&fp=" + urllib.parse.quote(fp)
    try:
        body = _get(url, f"{PASSPORT}/", jar, timeout)
    except Exception:
        return ""
    m = re.search(r"gen_callback\((\{.*\})\)", body)
    if not m:
        return ""
    try:
        payload = json.loads(m.group(1))
    except Exception:
        return ""
    tid = (payload.get("data") or {}).get("tid")
    if not tid:
        return ""
    # 第二步：用 tid 换 cookie
    u2 = (f"{PASSPORT}/visitor/visitor?a=incarnate&t={urllib.parse.quote(tid)}"
          f"&w=2&c=095&gc=&cb=cross_domain&from=weibo&_rand={random.random():.16f}")
    try:
        _get(u2, f"{PASSPORT}/", jar, timeout)
    except Exception:
        return ""
    parts = [f"{c.name}={c.value}" for c in jar if c.name in ("SUB", "SUBP", "SUBP_", "ALF")]
    return "; ".join(parts)


def get_cookie(user_cookie: str = "", force_refresh: bool = False) -> tuple[str, str]:
    """返回 (cookie串, 来源说明)。

    用户填了真实 cookie 就优先用它；否则自动签发访客身份。
    """
    if user_cookie and user_cookie.strip():
        return user_cookie.strip(), "已使用设置中填写的微博 Cookie"
    now = time.time()
    hit = _CACHE.get("visitor")
    if not force_refresh and hit and hit[0] > now:
        return hit[1], "已自动签发微博访客身份（无需账号）"
    ck = issue_visitor_cookie()
    if not ck:
        return "", "访客身份签发失败"
    _CACHE["visitor"] = (now + _TTL, ck)
    return ck, "已自动签发微博访客身份（无需账号）"


def invalidate() -> None:
    _CACHE.pop("visitor", None)
