"""平台接入注册表：每个平台的登录入口、凭据字段、接入方式与作用说明。

工具支持三种接入方式：
  1. none   —— 免登录，直接贴链接就能抓（Steam / Reddit）
  2. cookie —— 点「打开登录页」登录，再点「一键获取」自动写入 Cookie（或手动粘贴）
  3. key    —— 需要去开放平台申请 API Key（YouTube）

界面上的「平台接入」面板完全由这张表驱动，新增平台只改这里。
"""
from __future__ import annotations

# need: none=免登录 / optional=可不登录但登录后更好 / recommended=强烈建议 / required=必须
ACCESS: list[dict] = [
    {
        "key": "bilibili",
        "label": "哔哩哔哩",
        "kind": "cookie",
        "setting": "bilibili_cookie",
        "need": "optional",
        "login_marker": "SESSDATA",
        "login_url": "https://passport.bilibili.com/login",
        "home_url": "https://www.bilibili.com/",
        "domains": ["bilibili.com"],
        "no_login": "匿名可抓热评（WBI 接口，实测可翻数百条）",
        "after_login": "可翻完整评论区 + 楼中楼，抗风控更强（code -412 时必填）",
        "sample": "SESSDATA=xxxx; bili_jct=xxxx; DedeUserID=xxxx",
    },
    {
        "key": "weibo",
        "label": "微博",
        "kind": "cookie",
        "setting": "weibo_cookie",
        "need": "recommended",
        "login_marker": "SUB",
        "login_url": "https://passport.weibo.com/sso/signin?entry=miniblog",
        "home_url": "https://weibo.com/",
        "domains": ["weibo.com"],
        "no_login": "自动签发访客身份，能看到约 15 条评论",
        "after_login": "可翻页抓完整评论区（几百条）",
        "sample": "SUB=xxxx; SUBP=xxxx; ...",
    },
    {
        "key": "douyin",
        "label": "抖音",
        "kind": "cookie",
        "setting": "douyin_cookie",
        "need": "recommended",
        "login_marker": "sessionid",
        "login_url": "https://www.douyin.com/",
        "home_url": "https://www.douyin.com/",
        "domains": ["douyin.com"],
        "no_login": "服务端直连受签名风控（a_bogus），基本抓不到",
        "after_login": "带登录 Cookie 可尝试 web 评论接口；仍被风控时请用浏览器采集助手（在浏览器里跑，天然带完整环境）",
        "sample": "ttwid=xxxx; sessionid=xxxx; ...",
    },
    {
        "key": "xiaohongshu",
        "label": "小红书",
        "kind": "cookie",
        "setting": "xiaohongshu_cookie",
        "need": "recommended",
        "login_marker": "web_session",
        "login_url": "https://www.xiaohongshu.com/",
        "home_url": "https://www.xiaohongshu.com/",
        "domains": ["xiaohongshu.com"],
        "no_login": "接口需 x-s 签名，服务端连不上",
        "after_login": "有 Cookie 时会尝试一次评论接口；失败属正常，请用浏览器采集助手（登录态在浏览器里，成功率最高）",
        "sample": "web_session=xxxx; a1=xxxx; ...",
    },
    {
        "key": "tieba",
        "label": "百度贴吧",
        "kind": "cookie",
        "setting": "baidu_cookie",
        "need": "required",
        "login_marker": "BDUSS",
        "login_url": "https://passport.baidu.com/v2/?login",
        "home_url": "https://tieba.baidu.com/",
        "domains": ["baidu.com"],
        "no_login": "PC 页面与接口一律 403，抓不到任何楼层",
        "after_login": "带百度 Cookie 后可解析楼层内容",
        "sample": "BDUSS=xxxx; BAIDUID=xxxx; ...",
    },
    {
        "key": "taptap",
        "label": "TapTap",
        "kind": "cookie",
        "setting": "taptap_cookie",
        "need": "recommended",
        "login_marker": "PHPSESSID",
        "login_url": "https://www.taptap.cn/",
        "home_url": "https://www.taptap.cn/",
        "domains": ["taptap.cn"],
        "no_login": "评论接口带签名校验，未登录通常直接失败",
        "after_login": "可拉取评测列表与评分",
        "sample": "PHPSESSID=xxxx; ...",
    },
    {
        "key": "nga",
        "label": "NGA 论坛",
        "kind": "cookie",
        "setting": "nga_cookie",
        "need": "optional",
        "login_marker": "ngaPassportCid",
        "login_url": "https://bbs.nga.cn/nuke.php?__lib=login&__act=login_page",
        "home_url": "https://bbs.nga.cn/",
        "domains": ["nga.cn", "ngabbs.com"],
        "no_login": "可抓到公开版面楼层",
        "after_login": "可抓需要登录的版面/完整回复",
        "sample": "ngaPassportUid=xxxx; ngaPassportCid=xxxx; ...",
    },
    {
        "key": "steam",
        "label": "Steam 评测",
        "kind": "none",
        "setting": "",
        "need": "none",
        "login_url": "https://store.steampowered.com/",
        "home_url": "https://store.steampowered.com/",
        "domains": [],
        "no_login": "官方接口，贴商店链接或 AppID 即可抓全部语言评测",
        "after_login": "无需登录",
        "sample": "",
    },
    {
        "key": "reddit",
        "label": "Reddit",
        "kind": "none",
        "setting": "",
        "need": "none",
        "login_url": "https://www.reddit.com/",
        "home_url": "https://www.reddit.com/",
        "domains": [],
        "no_login": "公开 JSON 接口，贴帖子链接即可展开评论树",
        "after_login": "无需登录（国内访问可能超时）",
        "sample": "",
    },
    {
        "key": "youtube",
        "label": "YouTube",
        "kind": "key",
        "setting": "youtube_api_key",
        "need": "required",
        "login_url": "https://console.cloud.google.com/apis/credentials",
        "home_url": "https://www.youtube.com/",
        "domains": [],
        "no_login": "必须有 API Key 才能取评论",
        "after_login": "在 Google Cloud 建项目 → 启用 YouTube Data API v3 → 创建 API Key → 粘到下方",
        "sample": "AIzaSy...",
    },
]

BY_KEY = {a["key"]: a for a in ACCESS}


def cookie_for(settings: dict | None, platform_key: str) -> str:
    """按平台 key 取已保存的 Cookie（统一入口，所有采集器都走这里）。"""
    settings = settings or {}
    a = BY_KEY.get(platform_key)
    if not a or not a.get("setting"):
        return ""
    return str(settings.get(a["setting"]) or "").strip()


def status_of(settings: dict | None) -> dict[str, dict]:
    """返回每个平台的接入状态，供界面渲染。"""
    settings = settings or {}
    out: dict[str, dict] = {}
    for a in ACCESS:
        raw = str(settings.get(a["setting"]) or "").strip() if a["setting"] else ""
        if a["kind"] == "none":
            st = "free"
        elif raw:
            st = "linked"
        else:
            st = "none"
        out[a["key"]] = {"state": st, "value": raw, "masked": _mask(raw)}
    return out


def _mask(raw: str) -> str:
    """展示用脱敏：只保留字段名和末尾 4 位。"""
    if not raw:
        return ""
    parts = []
    for seg in raw.split(";"):
        seg = seg.strip()
        if not seg or "=" not in seg:
            continue
        k, _, v = seg.partition("=")
        v = v.strip()
        parts.append(f"{k}=…{v[-4:]}" if len(v) > 8 else f"{k}=***")
    return "; ".join(parts[:6]) + (" …" if len(parts) > 6 else "")
