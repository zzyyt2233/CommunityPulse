"""真实平台抓取实测：看目标条数 vs 实得条数，以及没抓满时的原因。

用法: venv\\Scripts\\python.exe tools\\live_test.py [目标条数]
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.collectors import route  # noqa: E402
from app.core.config import load_settings  # noqa: E402
from app.core.http_client import get_json  # noqa: E402


def pick_bilibili_video() -> str:
    """从 B站热门排行取一个视频链接，避免写死失效的 BV 号。"""
    data = get_json("https://api.bilibili.com/x/web-interface/ranking/v2?rid=0&type=all")
    for item in (data.get("data") or {}).get("list") or []:
        return f"https://www.bilibili.com/video/{item['bvid']}"
    return ""


def run(name: str, url: str | None, limit: int, sort: str = "hot") -> None:
    print(f"\n=== {name} | 目标 {limit} 条 | 排序 {sort} ===")
    if not url:
        print("  跳过：没有可用链接")
        return
    c = route(url)
    if c is None:
        print("  没有匹配的采集器")
        return
    t0 = time.time()
    try:
        res = c.collect(url, limit=limit, settings=load_settings(), sort=sort)
    except Exception as exc:  # noqa: BLE001
        print(f"  抓取异常: {type(exc).__name__}: {exc}")
        return
    cost = time.time() - t0
    got = len(res.comments)
    flag = "已抓满" if res.complete else "未抓满"
    print(f"  平台 {c.label}({res.reliability}) 实得 {got}/{limit} 条 · {flag} · 耗时 {cost:.1f}s")
    if res.source_title:
        print(f"  来源: {res.source_title[:50]}")
    if not res.complete and res.stop_reason:
        print(f"  原因: {res.stop_reason}")
    if res.capacity_hint:
        print(f"  说明: {res.capacity_hint}")
    if got:
        top = sorted(res.comments, key=lambda x: -x.like_count)[:3]
        for c2 in top:
            print(f"    👍{c2.like_count:<6} {c2.content[:46]}")


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    bv = pick_bilibili_video()
    if bv:
        print("B站热门视频:", bv)
    run("B站", bv, limit, "hot")
    run("Steam", "https://store.steampowered.com/app/570", limit, "hot")
    return 0


if __name__ == "__main__":
    sys.exit(main())
