"""B站 WBI 签名接口。

为什么需要它：B站老的评论接口 `/x/v2/reply` 对未登录访客只返回 **3 条主评论**，
第 2 页起返回空数组但 `code=0`（静默给空，不报错），导致工具看起来"只能抓几条"。
新的 WBI 签名接口 `/x/v2/reply/wbi/main` 同样匿名，但一页 20 条、可以一直翻，
实测同一视频从 9 条提升到 1400+ 条。

签名算法（公开标准，非逆向黑盒）：
1. `/x/web-interface/nav` 取 `wbi_img.img_url` / `sub_url` 的文件名作为 img_key / sub_key
2. 两个 key 拼接后按固定重排表取前 32 位得到 mixin_key
3. 所有参数按 key 升序、过滤掉 `!'()*` 后 urlencode，追加 `wts`（时间戳）
4. `w_rid = md5(query_string + mixin_key)`

踩过的坑（都在真机请求里暴露的，看不出来）：
- `pagination_str` 必须用**紧凑 JSON**（无空格）。`json.dumps` 默认带空格，服务端解析不了，
  表现为第 1 页能拿到 20 条、第 2 页 0 条。
- 下一页偏移量在 **`cursor.pagination_reply.next_offset`**，不是 `cursor.pagination_str`
  （后者的 offset 是我一开始猜的字段，实际返回到 @type 结构里）。
- key 会定期更换，但同一进程内缓存即可；缓存失效时重新拉取。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.parse

from ..core.http_client import get_json

# 固定的密钥重排表（B站公开的常量）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

API_REPLY_WBI = "https://api.bilibili.com/x/v2/reply/wbi/main"
API_NAV = "https://api.bilibili.com/x/web-interface/nav"

_keys: tuple[str, str] | None = None
_keys_ts: float = 0.0
_KEY_TTL = 3600.0  # 密钥定期更换，1 小时后重新拉取


def _fetch_keys() -> tuple[str, str]:
    global _keys, _keys_ts
    now = time.time()
    if _keys and now - _keys_ts < _KEY_TTL:
        return _keys
    data = get_json(API_NAV)
    wbi = (data.get("data") or {}).get("wbi_img") or {}
    img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img_key or not sub_key:
        _keys, _keys_ts = None, 0.0
        raise RuntimeError("WBI 密钥获取失败（可能需要 Cookie 或触发风控）")
    _keys, _keys_ts = (img_key, sub_key), now
    return _keys


def _mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


def sign(params: dict) -> str:
    """对参数做 WBI 签名，返回可直接拼在 URL 后的 query string。"""
    img_key, sub_key = _fetch_keys()
    mk = _mixin_key(img_key, sub_key)
    p = dict(params)
    p["wts"] = int(time.time())
    # 过滤特殊字符后按 key 升序
    clean = {k: "".join(ch for ch in str(v) if ch not in "!'()*") for k, v in p.items()}
    # 必须用 quote 而不是默认的 quote_plus：B站不接受空格被编码成 "+"，
    # 搜索多词关键词（如「原神 卡顿」）会静默返回 0 条结果。
    query = urllib.parse.urlencode(dict(sorted(clean.items())), quote_via=urllib.parse.quote)
    w_rid = hashlib.md5((query + mk).encode("utf-8")).hexdigest()
    return f"{query}&w_rid={w_rid}"


def fetch_page(oid: str, rtype: int, mode: int, offset: str = "",
               headers: dict | None = None) -> dict:
    """拉取一页评论。

    mode: 2=按时间（最新），3=按热度（热门）
    返回原始 data 字典（含 replies / cursor / top_replies）。
    """
    pagination = json.dumps({"offset": offset}, separators=(",", ":"))
    query = sign({
        "oid": oid,
        "type": rtype,
        "mode": mode,
        "pagination_str": pagination,
        "plat": 1,
        "web_location": 1315875,
    })
    data = get_json(f"{API_REPLY_WBI}?{query}", headers=headers or {})
    code = data.get("code")
    if code != 0:
        raise RuntimeError(f"WBI 接口返回 code {code}（{data.get('message')}）")
    return data.get("data") or {}


def next_offset(payload: dict) -> tuple[str, bool]:
    """从返回体取下页偏移量，返回 (offset, is_end)。"""
    cur = payload.get("cursor") or {}
    is_end = bool(cur.get("is_end"))
    off = (cur.get("pagination_reply") or {}).get("next_offset") or ""
    return off, is_end


def total_count(payload: dict) -> int:
    """评论总数（cursor.all_count）。"""
    return int((payload.get("cursor") or {}).get("all_count") or 0)
