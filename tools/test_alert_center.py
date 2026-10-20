"""预警中心：前端面板挂得上、后端返回的字段对得上前端渲染要用的。

面板是死的（HTML/CSS/id），接口是活的（字段/过滤/已读）——
两边都得验，只验一边会出现「接口通了但页面空白」这种假完成。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ok(label: str, cond: bool) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ " + label)
    else:
        FAIL += 1
        print("  ✗ " + label)


def hr(t: str) -> None:
    print("\n" + t)


def read(p: str) -> str:
    with open(os.path.join(ROOT, p), "r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    html = read("web/index.html")
    css = read("web/style.css")
    js = read("web/app.js")

    hr("1 · 面板真的在页面上")
    ok("有「预警中心」标题", "预警中心" in html)
    idx_html = html.find("预警中心")
    idx_watch = html.find("监测清单")
    ok("排在监测清单之后（同属任务页）", idx_watch >= 0 and idx_html > idx_watch)
    for i in ("alertList", "alertCount", "btnAlertUnread", "btnAlertReadAll"):
        ok(f"HTML 里有 #{i}", f'id="{i}"' in html)
    for i in ("alertList", "alertCount", "btnAlertUnread", "btnAlertReadAll"):
        ok(f"app.js 引用了 #{i}", f"'#{i}'" in js or f'"{i}"' in js)

    hr("2 · 样式定义了前端会用到的类")
    for c in (".alert-list", ".alert-item", ".alert-item.high",
              ".alert-item.medium", ".alert-item.read", ".alert-lv"):
        ok(f"CSS 有 {c}", c in css)

    hr("3 · JS 调的接口路径与后端一致")
    ok("读列表走 /api/watch-alerts", "/api/watch-alerts" in js)
    ok("标已读走 /api/watch-alerts/read", "/api/watch-alerts/read" in js)
    ok("支持 unread 过滤参数", "unread=1" in js)
    ok("有定时刷新（不是只加载一次）", "setInterval(loadAlerts" in js)

    # ---- 活的一面：真起服务 ----
    from app import server
    from app.core import db

    port = 8907
    t = threading.Thread(target=lambda: server.serve(open_browser=False, port=port),
                         daemon=True)
    t.start()
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2)
            break
        except Exception:
            time.sleep(0.5)

    def call(path, payload=None, method=None):
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        h = {"Content-Type": "application/json"} if data else {}
        m = method or ("POST" if data else "GET")
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                   headers=h, method=m)
        return json.loads(op.open(r, timeout=20).read().decode())

    try:
        hr("4 · 后端返回的字段，正是前端渲染要用的")
        with db.connect() as c:
            c.execute("DELETE FROM watch_alerts")
        db.add_watch_alert(1, 11, "high", "负面率突然升高", "负面占比 42%，超过 25% 阈值")
        db.add_watch_alert(1, 12, "medium", "声量异常放大", "本轮新增 300 条")
        j = call("/api/watch-alerts?limit=100")
        ok("接口返回 ok", j.get("ok") is True)
        items = j.get("items", [])
        ok("返回了 2 条", len(items) == 2)
        need = {"id", "level", "title", "detail", "created_at", "is_read"}
        ok("每条都含前端要的全部字段", all(need <= set(x) for x in items))
        ok("level 是前端认识的值", all(x["level"] in ("high", "medium") for x in items))
        ok("created_at 是秒级时间戳（前端按 *1000 转日期）",
           all(0 < x["created_at"] < 1e11 for x in items))
        ok("未读计数正确", j.get("unread") == 2)

        hr("5 · 只看未读 / 标已读真的生效")
        call("/api/watch-alerts/read", {"ids": [items[0]["id"]]})
        j = call("/api/watch-alerts?limit=100")
        ok("未读降到 1", j.get("unread") == 1)
        j2 = call("/api/watch-alerts?limit=100&unread=1")
        ok("unread=1 只返回 1 条", len(j2.get("items", [])) == 1)
        # 上面把 items[0] 标了已读，未读只剩另一条。用 id 比对，不依赖时间排序。
        ok("返回的是未读那条（按 id 比对）",
           [x["id"] for x in j2.get("items", [])] == [items[1]["id"]])
        r = call("/api/watch-alerts/read", {})
        ok("全部已读返回更新条数", r.get("ok") is True and r.get("updated") == 1)
        j = call("/api/watch-alerts?limit=100")
        ok("未读归零", j.get("unread") == 0)
    finally:
        with db.connect() as c:
            c.execute("DELETE FROM watch_alerts")

    print(f"\n通过 {PASS} / 失败 {FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
