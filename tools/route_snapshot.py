"""路由快照：把全部 40 个路由的真实响应存成 JSON，重构前后各跑一次做 diff。

这是重构的保险丝 —— 只靠「测试通过」不够，快照能抓出
「路由还能通但行为悄悄变了」这类回归。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"

# (方法, 路径, body) —— 覆盖 _api_get 17 条 + _api_post 23 条 + 静态 404 兜底
CASES = [
    ("GET", "/api/health", None),
    ("GET", "/api/watchlist", None),
    ("GET", "/api/watchlist/1/history", None),
    ("GET", "/api/watch-alerts", None),
    ("GET", "/api/watch-alerts?unread=1&limit=5", None),
    ("GET", "/api/browser-capture/nope", None),
    ("GET", "/api/browser-keyword-collect/nope", None),
    ("GET", "/api/platforms", None),
    ("GET", "/api/credentials", None),
    ("GET", "/api/tasks", None),
    ("GET", "/api/tasks/999999", None),
    ("GET", "/api/analysis/999999", None),
    ("GET", "/api/export/不存在", None),
    ("GET", "/exports/不存在", None),
    ("GET", "/api/settings", None),
    ("GET", "/api/sentiment-memory", None),
    ("GET", "/api/comment-memory", None),
    ("GET", "/api/comments/999999", None),
    ("GET", "/api/unknown-route", None),
    ("POST", "/api/tasks", {"name": "快照", "urls": ["https://example.invalid/x"]}),
    ("POST", "/api/watchlist", {"name": "快照监测", "url": "https://example.invalid/y",
                                 "interval_hours": 6}),
    ("POST", "/api/watchlist/999999/toggle", None),
    ("POST", "/api/watchlist/999999/remove", None),
    ("POST", "/api/watchlist/999999/run", None),
    ("POST", "/api/watch-alerts/read", {"ids": []}),
    ("POST", "/api/browser-capture", {"platform": "bilibili", "url": "x"}),
    ("POST", "/api/discover", {"platform": "bilibili", "keyword": "快照"}),
    ("POST", "/api/keyword-collect", {"platform": "bilibili", "keyword": "快照"}),
    ("POST", "/api/tasks/999999/recollect", None),
    ("POST", "/api/preview", {"url": "https://example.invalid/x"}),
    ("POST", "/api/import", {"text": "第一条评论\n第二条评论", "name": "快照导入"}),
    ("POST", "/api/analyze/999999", None),
    ("POST", "/api/settings", {"steam_lang": "all"}),
    ("POST", "/api/sentiment-memory", {"text": "测试语句", "label": "positive"}),
    ("POST", "/api/comment-sentiment", {"text": "测试评论", "label": "positive"}),
    ("POST", "/api/comment-memory/delete", {"key": "不存在"}),
    ("POST", "/api/credentials/open", {"platform": "bilibili"}),
    ("POST", "/api/credentials/capture", {"platform": "bilibili"}),
    ("POST", "/api/credentials/save", {"platform": "bilibili", "cookie": ""}),
    ("POST", "/api/credentials/clear", {"platform": "bilibili"}),
    ("POST", "/api/credentials/close", {"platform": "bilibili"}),
    ("POST", "/api/merge", {"ids": []}),
    ("POST", "/api/unknown-route", None),
]


def snap() -> dict:
    from app import server
    from app.core import db

    # 先把库清到同一基座，否则 before/after 两次快照会比到
    # 「第一次跑时残留的历史数据」而不是行为差异。
    with db.connect() as c:
        c.execute("DELETE FROM watch_alerts")
        c.execute("DELETE FROM watch_snapshots")
        c.execute("DELETE FROM watchlist")
        for row in c.execute("SELECT id FROM tasks").fetchall():
            c.execute("DELETE FROM comments WHERE task_id=?", (row["id"],))
            c.execute("DELETE FROM analyses WHERE task_id=?", (row["id"],))
            c.execute("DELETE FROM tasks WHERE id=?", (row["id"],))

    t = threading.Thread(target=lambda: server.serve(open_browser=False, port=PORT),
                         daemon=True)
    t.start()
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/api/health", timeout=2)
            break
        except Exception:
            time.sleep(0.5)

    out = {}
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for method, path, body in CASES:
        key = f"{method} {path}"
        try:
            data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
            h = {"Content-Type": "application/json"} if data else {}
            r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
            with op.open(r, timeout=60) as resp:
                code, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        except Exception as e:  # noqa: BLE001
            out[key] = {"status": "EXC", "body": f"{type(e).__name__}: {e}"[:200]}
            continue
        try:
            j = json.loads(raw.decode("utf-8"))
            # 只留形状：ok / error 是否存在、列表长度、关键标量
            shape = {"ok": j.get("ok")} if isinstance(j, dict) else {"type": type(j).__name__}
            if isinstance(j, dict):
                for k in ("items", "total", "unread", "updated", "removed", "count",
                          "task_id", "error", "message"):
                    if k in j:
                        v = j[k]
                        shape[k] = len(v) if isinstance(v, list) else v
            out[key] = {"status": code, "body": shape}
        except Exception:
            out[key] = {"status": code, "body": raw[:120].decode("utf-8", "replace")}

    # 快照本身会在库里留下任务/记忆，跑完即清，别污染真实数据
    with db.connect() as c:
        c.execute("DELETE FROM watch_alerts")
        c.execute("DELETE FROM watch_snapshots")
        c.execute("DELETE FROM watchlist")
        for row in c.execute("SELECT id FROM tasks").fetchall():
            c.execute("DELETE FROM comments WHERE task_id=?", (row["id"],))
            c.execute("DELETE FROM analyses WHERE task_id=?", (row["id"],))
            c.execute("DELETE FROM tasks WHERE id=?", (row["id"],))
    return out


if __name__ == "__main__":
    target = sys.argv[1]
    json.dump(snap(), open(target, "w", encoding="utf-8"), ensure_ascii=False,
              indent=1, sort_keys=True)
    print("snapshot ->", target)
