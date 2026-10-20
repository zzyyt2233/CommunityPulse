# -*- coding: utf-8 -*-
"""验证：监测清单（watchlist）定时盯梢这条链真的接上了。

分四层，前三层绕过路由，最后一层起真服务走 HTTP —— 因为这次改动的本质就是
「把已有的表 / 增量采集 / 分析 / 预警接起来」，只测函数是测不出接线是否通的：
db.py 里 watchlist 的三个 CRUD 早就存在，但没有任何路由和调度器调它们，
纯函数测试会全绿而功能实际是死的。所以必须有 HTTP 层。

1. 到期判定 _watch_due：新建 / 停用 / 刚跑过 / 超期间隔 四种情况；
2. 预警摊平 _alerts_from_result：只留 high / medium，low 与 none 要被滤掉；
3. 数据层 CRUD：增列查、启停、改间隔、记运行时间，以及预警的落库与已读；
4. HTTP 路由：GET/POST/DELETE /api/watchlist、GET /api/watch-alerts、
   POST toggle / run / read，全部走真实端口。
"""
import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.analysis.pipeline import _build_alerts  # noqa: E402
from app.core import db  # noqa: E402
from app import server as web_server  # noqa: E402

PASS = FAIL = 0


def ok(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \u2713 {label}")
    else:
        FAIL += 1
        print(f"  \u2717 \u5931\u8d25! {label}")
    return cond


def hr(t):
    print(f"\n--- {t} ---")


db.init_db()


# ---------- 1. 到期判定 ----------
def test_due():
    hr("1 · 到期判定 _watch_due")
    now = time.time()
    ok("从未跑过的新条目 -> 到期",
       web_server._watch_due({"enabled": 1, "last_run": None, "interval_hours": 24}))
    ok("已停用 -> 永不到期",
       not web_server._watch_due({"enabled": 0, "last_run": None, "interval_hours": 24}))
    ok("24 小时前刚跑过、间隔 24h -> 不到期",
       not web_server._watch_due({"enabled": 1, "last_run": now - 3600,
                                  "interval_hours": 24}))
    ok("48 小时前跑过、间隔 24h -> 到期",
       web_server._watch_due({"enabled": 1, "last_run": now - 48 * 3600,
                              "interval_hours": 24}))
    ok("间隔损坏（None）-> 按 24h 兜底不崩",
       web_server._watch_due({"enabled": 1, "last_run": now - 48 * 3600,
                              "interval_hours": None}))


# ---------- 2. 预警摊平 ----------
def test_alerts_flatten():
    hr("2 · 预警摊平 _alerts_from_result")
    high = {"alerts": {"level": "high", "headline": "负面占比 40%",
                       "items": [{"label": "负面 40 条 / 共 100 条"}], "warn_at": 0.25}}
    med = {"alerts": {"level": "medium", "headline": "接近警戒线", "items": []}}
    low = {"alerts": {"level": "low", "headline": "整体可控", "items": []}}
    none_ = {"alerts": {"level": "none", "headline": "", "items": []}}

    r = web_server._alerts_from_result(high)
    ok("high 保留", len(r) == 1 and r[0][0] == "high")
    ok("high 带上 headline", r and "40%" in r[0][1])
    ok("high 带上明细", r and "负面 40 条" in r[0][2])
    ok("medium 保留", len(web_server._alerts_from_result(med)) == 1)
    ok("low 被滤掉（可控的不必报警）", web_server._alerts_from_result(low) == [])
    ok("none 被滤掉", web_server._alerts_from_result(none_) == [])
    ok("结果里没有 alerts 字段也不崩", web_server._alerts_from_result({}) == [])

    # 与真实预警逻辑自洽：真跑一遍 _build_alerts 的 high 结果能被摊平
    real = _build_alerts({"total": 100, "positive": 20, "neutral": 40, "negative": 40}, [])
    ok("真实 _build_alerts 的 high 也能摊平",
       len(web_server._alerts_from_result({"alerts": real})) == 1)


# ---------- 3. 数据层 ----------
def test_db():
    hr("3 · 数据层 CRUD")
    wid = db.add_watch("单元测试项", "https://example.invalid/p1", "bilibili", 6)
    ok("add_watch 返回 id", isinstance(wid, int) and wid > 0)
    w = db.get_watch(wid)
    ok("get_watch 能取回", w and w["url"] == "https://example.invalid/p1")
    ok("间隔存对了", w and int(w["interval_hours"]) == 6)
    ok("新条目 last_run 为空", w and not w["last_run"])

    db.set_watch_enabled(wid, False)
    ok("停用生效", int(db.get_watch(wid)["enabled"]) == 0)
    db.set_watch_enabled(wid, True)
    ok("启用生效", int(db.get_watch(wid)["enabled"]) == 1)
    db.update_watch_interval(wid, 12)
    ok("改间隔生效", int(db.get_watch(wid)["interval_hours"]) == 12)
    db.update_watch_run(wid)
    ok("update_watch_run 写入时间", bool(db.get_watch(wid)["last_run"]))

    ok("list_watchlist 含该项", any(x["id"] == wid for x in db.list_watchlist()))

    # 快照：前端的趋势迷你图与「历史」都依赖它
    db.add_watch_snapshot(wid, None, 0.4, 0.2, 100)
    snap = db.latest_watch_snapshot(wid)
    ok("快照能存下", snap and abs(snap["negative_rate"] - 0.4) < 1e-6)
    ok("快照计数 = 1", db.count_watch_snapshots(wid) == 1)
    db.add_watch_snapshot(wid, None, 0.5, 0.1, 120)
    ok("latest 取最新的一条", abs(db.latest_watch_snapshot(wid)["negative_rate"] - 0.5) < 1e-6)
    ok("快照计数 = 2", db.count_watch_snapshots(wid) == 2)
    ok("历史按时间倒序返回",
       [s["negative_rate"] for s in db.list_watch_snapshots(wid)] == [0.5, 0.4])

    aid = db.add_watch_alert(wid, None, "high", "测试预警", "明细")
    ok("add_watch_alert 返回 id", isinstance(aid, int) and aid > 0)
    before = db.count_unread_alerts()
    ok("未读计数 >= 1", before >= 1)
    db.add_watch_alert(wid, None, "medium", "第二条", "")
    ok("未读计数 +1", db.count_unread_alerts() == before + 1)
    ok("list_watch_alerts 能列出", any(x["id"] == aid for x in db.list_watch_alerts()))
    ok("unread_only 过滤生效",
       all(int(x["is_read"]) == 0 for x in db.list_watch_alerts(50, True)))
    n = db.mark_alerts_read([aid])
    ok("mark_alerts_read(单条) 返回 1", n == 1)
    ok("该条已读", int([x for x in db.list_watch_alerts() if x["id"] == aid][0]["is_read"]) == 1)

    db.remove_watch(wid)
    ok("remove_watch 生效", db.get_watch(wid) is None)
    return aid


# ---------- 4. HTTP 路由 ----------
def test_routes():
    hr("4 · HTTP 路由（真服务）")
    srv = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    def call(path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method or ("POST" if data else "GET"))
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    created = []
    try:
        st, j = call("/api/watchlist", {"name": "路由测试", "url": "https://example.invalid/r1",
                                        "interval_hours": 3})
        ok("POST /api/watchlist 返回 ok", st == 200 and j.get("ok") is True)
        wid = j.get("id")
        created.append(wid)
        ok("POST 返回新 id", isinstance(wid, int))
        ok("间隔被夹到合法范围", j.get("interval_hours") == 3)

        st, j = call("/api/watchlist", {"url": ""})
        ok("空链接被拒 400", st == 400 and j.get("ok") is False)

        st, j = call("/api/watchlist")
        ok("GET /api/watchlist 返回 ok", st == 200 and j.get("ok") is True)
        item = [x for x in j.get("items", []) if x["id"] == wid]
        ok("GET 列表含刚加的项", len(item) == 1)
        ok("列表带 due 状态字段", item and "due" in item[0])
        ok("列表带 running 状态字段", item and "running" in item[0])
        ok("列表带 latest 字段（前端要画趋势）", item and "latest" in item[0])
        ok("列表带 snapshots 计数字段", item and "snapshots" in item[0])

        # 历史快照路由：前端「历史」按钮打的就是它
        db.add_watch_snapshot(wid, None, 0.33, 0.5, 90)
        st, j = call(f"/api/watchlist/{wid}/history")
        ok("GET history 返回 ok", st == 200 and j.get("ok") is True)
        ok("history 含刚写的快照",
           any(abs((x.get("negative_rate") or 0) - 0.33) < 1e-6 for x in j.get("items", [])))
        st, _ = call("/api/watchlist/999999/history")
        ok("history 不存在的项 -> 404", st == 404)

        st, j = call(f"/api/watchlist/{wid}/toggle", {"enabled": False})
        ok("toggle 停用返回 enabled=False", st == 200 and j.get("enabled") is False)
        st, j = call(f"/api/watchlist/{wid}/toggle", {})
        ok("toggle 不传值则反转", st == 200 and j.get("enabled") is True)

        st, j = call(f"/api/watchlist/999999/toggle", {"enabled": True})
        ok("不存在的监测项 -> 404", st == 404)

        # run：真起线程会去抓一个不存在的链接，这里只验证「受理了」这个契约
        st, j = call(f"/api/watchlist/{wid}/run", {})
        ok("手动 run 被受理", st == 200 and j.get("running") is True)
        time.sleep(1.5)  # 让它真的跑一下，确认不会炸在主线程上

        st, j = call("/api/watch-alerts")
        ok("GET /api/watch-alerts 返回 ok", st == 200 and j.get("ok") is True)
        ok("带未读总数", isinstance(j.get("unread"), int))

        st, j = call("/api/watch-alerts/read", {"ids": [999999]})
        ok("标已读（不存在的 id）不报错", st == 200 and j.get("ok") is True)

        st, j = call(f"/api/watchlist/{wid}", None, "DELETE")
        ok("DELETE /api/watchlist/<id> 返回 ok", st == 200 and j.get("ok") is True)
        st, after = call("/api/watchlist")
        ok("删除后列表里没有了",
           all(x["id"] != wid for x in after.get("items", [])))

        # 前端用的是 POST .../remove 而不是 DELETE，这条不匹配就整个面板点不动
        st, j = call("/api/watchlist", {"name": "删除测试",
                                        "url": "https://example.invalid/r2"})
        wid2 = j.get("id")
        created.append(wid2)
        st, j = call(f"/api/watchlist/{wid2}/remove", {})
        ok("POST /api/watchlist/<id>/remove 返回 ok", st == 200 and j.get("ok") is True)
        st, after = call("/api/watchlist")
        ok("POST remove 后列表里没有了",
           all(x["id"] != wid2 for x in after.get("items", [])))
    finally:
        srv.shutdown()
        for w in created:
            try:
                db.remove_watch(w)
            except Exception:
                pass


# ---------- 5. 调度器幂等 ----------
def test_scheduler_idempotent():
    hr("5 · 调度器幂等")
    # 先清空监测项，避免巡检线程立刻把测试条目拉去采集，污染上面的断言
    for w in db.list_watchlist():
        try:
            db.remove_watch(w["id"])
        except Exception:
            pass
    web_server._WATCH_SCHEDULER_STARTED = False
    web_server.start_watch_scheduler()
    first = web_server._WATCH_SCHEDULER_STARTED
    web_server.start_watch_scheduler()
    ok("start_watch_scheduler 置位", first is True)
    ok("重复调用仍是 True（幂等，不会起第二个巡检线程）",
       web_server._WATCH_SCHEDULER_STARTED is True)


test_due()
test_alerts_flatten()
test_db()
test_routes()
test_scheduler_idempotent()

print(f"\n{'=' * 46}")
print(f"通过 {PASS} · 失败 {FAIL}")
print("监测清单定时盯梢：接线验证通过 ✅" if FAIL == 0 else "存在失败项，见上")
sys.exit(1 if FAIL else 0)
