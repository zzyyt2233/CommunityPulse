# -*- coding: utf-8 -*-
"""离线验证：负面舆情预警（app/analysis/pipeline.py 的 _build_alerts）。

分四层：
1. 纯逻辑 —— 直接喂 stats / clusters，判定等级、阈值、话题筛选是否按预期；
2. 端到端 —— 真跑一遍 analyze，确认 alerts 出现在结果里且与 stats 自洽；
3. 导出报告 —— 预警条要真的出现在 HTML 里，并且内容做过转义；
4. 导出路由 —— 起一个真服务走 HTTP，确认 GET /api/export/<id>?fmt=html 这条链通。
   前三层都绕过路由直接调函数，路由出问题时它们全是绿的，所以单列一层。
"""
import io
import json
import pathlib
import shutil
import sys
import threading

from urllib.request import urlopen

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.analysis.pipeline import _build_alerts, analyze  # noqa: E402
from app.core import db  # noqa: E402
from app import server as web_server  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def ok(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ 失败! {label}")
    return cond


def st(total, neg, pos=None, neu=None):
    pos = pos if pos is not None else (total - neg) // 2
    neu = neu if neu is not None else total - neg - pos
    return {"total": total, "negative": neg, "positive": pos, "neutral": neu}


def _empty_alerts_shape():
    """没有可分析内容时走的是 _empty_result，那条路径也得带上 alerts。"""
    e = analyze([], {})
    a = e.get("alerts") or {}
    return a.get("level") == "none" and a.get("items") == [] and "warn_at" in a


def main():
    print("== 纯逻辑 ==")
    a = _build_alerts(st(100, 40), [])
    ok("负面 40% → high", a["level"] == "high")
    ok("headline 带上占比与警戒线",
       "40.0%" in a["headline"] and "25%" in a["headline"])
    ok("默认警戒线是 25%", a["warn_at"] == 0.25)

    a2 = _build_alerts(st(100, 20), [])
    ok("负面 20% → medium（越过 60% 警戒线但没到线）", a2["level"] == "medium")

    a3 = _build_alerts(st(100, 5), [])
    ok("负面 5% → low", a3["level"] == "low")

    a4 = _build_alerts({"total": 0}, [])
    ok("没有数据 → none 且不报错", a4["level"] == "none" and a4["items"] == [])

    a5 = _build_alerts(st(100, 20), [], 0.1)
    ok("自定义阈值 10% 时，20% 算 high", a5["level"] == "high")
    a6 = _build_alerts(st(100, 20), [], 0.99)
    ok("阈值超过上限时被夹到 0.9，不会永远报 low", a6["warn_at"] == 0.9)
    a7 = _build_alerts(st(100, 20), [], "不是数字")
    ok("阈值传了脏值就退回默认", a7["warn_at"] == 0.25)

    clusters = [
        {"size": 10, "sentiment": {"negative": 8, "positive": 1, "neutral": 1},
         "keywords": ["优化", "闪退"], "representative": "优化太差一直闪退，官方也不修"},
        {"size": 12, "sentiment": {"negative": 2, "positive": 9, "neutral": 1},
         "keywords": ["画面"], "representative": "画面精美剧情很棒"},
        {"size": 2, "sentiment": {"negative": 2, "positive": 0, "neutral": 0},
         "keywords": ["客服"], "representative": "客服态度很差"},
        {"size": 6, "sentiment": {"negative": 1, "positive": 4, "neutral": 1},
         "keywords": ["音乐"], "representative": "音乐好听"},
    ]
    a8 = _build_alerts(st(100, 30), clusters)
    labels = [i["label"] for i in a8["items"]]
    ok("列出负面扎堆的话题", any(i["kind"] == "cluster" for i in a8["items"]))
    ok("话题标签里带关键词", any("优化" in s for s in labels))
    ok("条数太少的簇不列（2 条的直接略过，避免噪音）",
       not any("客服" in s for s in labels))
    ok("负面占比不高的簇不列", not any("画面" in s for s in labels))
    ok("headline 点名了负面话题数量", "负面集中在" in a8["headline"])

    print("== 端到端 ==")
    comments = [
        {"content": "优化太差了一直闪退，官方也不修", "like_count": 30},
        {"content": "这游戏优化太差，服务器还炸了", "like_count": 25},
        {"content": "画面精美但是优化拉胯，玩不下去", "like_count": 18},
        {"content": "服务器一直炸，优化太差了吧", "like_count": 12},
        {"content": "客服态度很差，退款也不给处理", "like_count": 9},
        {"content": "充值之后账号被封了，太离谱", "like_count": 7},
        {"content": "这游戏真不错，画面和剧情都很棒", "like_count": 40},
        {"content": "客服态度很好，处理速度很快", "like_count": 22},
        {"content": "音乐和配音都很用心，值得一玩", "like_count": 15},
        {"content": "新手引导做得很细致，上手很快", "like_count": 11},
        {"content": "打击感很棒，玩起来很爽", "like_count": 8},
        {"content": "我今天玩的是 PC 端", "like_count": 2},
        {"content": "刚更新了 1.2 版本", "like_count": 1},
        {"content": "楼主说得有道理", "like_count": 1},
        {"content": "我就是来看看评论的", "like_count": 0},
    ]
    r = analyze(comments, {})
    ok("分析结果里有 alerts 字段", "alerts" in r)
    al = r["alerts"]
    rate = r["stats"]["negative_rate"]
    expect = ("high" if rate >= al["warn_at"]
              else "medium" if rate >= al["warn_at"] * 0.6 else "low")
    ok(f"等级与负面占比自洽（rate={rate:.1%} → {al['level']}）", al["level"] == expect)
    ok("headline 里的占比与 stats 一致", f"{rate:.1%}" in al["headline"])
    ok("第一条预警项就是负面占比",
       al["items"] and al["items"][0]["kind"] == "rate")
    ok("预警项条数不会失控（最多 1 + 3）", len(al["items"]) <= 4)

    ok("空结果也带 alerts，前端不用额外判空",
       _empty_alerts_shape())

    print("== 导出报告 ==")
    from app.export import exporter  # noqa: E402

    d = ROOT / "temp" / "neg_alert_export"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    orig = exporter.EXPORT_DIR
    exporter.EXPORT_DIR = d
    try:
        # 报告里的评论内容原样来自抓取，必须当不可信字符串处理
        risky = dict(r)
        risky["alerts"] = {
            "level": "high",
            "headline": '负面 <b>越线</b> & "严重"',
            "items": [{"kind": "cluster", "label": "「优化」<script>x</script>",
                       "detail": "a & b"}],
            "warn_at": 0.25,
        }
        html = exporter.to_html_report("预警测试", risky, comments).read_text(
            encoding="utf-8")
        ok("报告里有预警条", 'class="alert high"' in html)
        ok("预警条排在 KPI 之前（先结论后数据）",
           html.index('class="alert high"') < html.index('class="kpis"'))
        ok("headline 被转义，没有裸标签", "<b>越线</b>" not in html and "&lt;b&gt;" in html)
        ok("预警项里的 script 被转义", "<script>x</script>" not in html)
        ok("level=low 也能出预警条", "alert low" in exporter.to_html_report(
            "预警测试低", {**r, "alerts": {"level": "low", "headline": "可控",
                                          "items": []}}, comments).read_text(encoding="utf-8"))
        ok("没有 alerts 字段时不炸也不出条",
           'class="alert' not in exporter.to_html_report(
               "预警测试空", {k: v for k, v in r.items() if k != "alerts"},
               comments).read_text(encoding="utf-8"))
    finally:
        exporter.EXPORT_DIR = orig
        shutil.rmtree(d, ignore_errors=True)

    print("== 导出路由 ==")
    # 上面是直接调 exporter，绕过了 HTTP 分发；而路由层恰好出过一次坑：
    # 导出只挂在 do_GET 上，用 POST 打会拿到「未知接口」404。
    srv = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    tid = None
    try:
        base = f"http://127.0.0.1:{port}"
        tid = db.create_task("路由测试", "bilibili", "https://example.invalid/x")
        db.insert_comments(tid, [{"platform": "bilibili", "cid": "r1",
                                  "content": "差评", "like_count": 1,
                                  "sentiment": "negative", "score": -0.8}])
        db.save_analysis(tid, {}, {
            "stats": {"total": 1, "positive": 0, "neutral": 0,
                      "negative": 1, "negative_rate": 1.0},
            "alerts": {"level": "high", "headline": "测试",
                       "items": [], "warn_at": 0.25}})
        raw = urlopen(f"{base}/api/export/{tid}?fmt=html", timeout=10).read()
        j = json.loads(raw)
        ok("GET /api/export/<id>?fmt=html 返回 ok", j.get("ok") is True)
        url = j.get("url") or ""
        ok("返回的下载 url 是 /exports/ 下的 html 文件",
           url.startswith("/exports/") and url.endswith(".html"))
        page = urlopen(base + url, timeout=10).read().decode("utf-8")
        ok("下载到的正是带预警条的报告", 'class="alert high"' in page)
        ok("下载 url 已百分号编码（无裸中文，地址栏直接可用）",
           "%" in url and all(ord(c) < 128 for c in url))
        # fmt 是 query 参数不是路径段；漏传时应该落到默认的 xlsx，而不是报错
        j2 = json.loads(urlopen(f"{base}/api/export/{tid}", timeout=10).read())
        ok("省略 fmt 时默认导出 xlsx", (j2.get("url") or "").endswith(".xlsx"))
        j3 = json.loads(urlopen(f"{base}/api/export/{tid}?fmt=csv",
                                timeout=10).read())
        ok("fmt=csv 走 CSV 分支", (j3.get("url") or "").endswith(".csv"))
    finally:
        srv.shutdown()
        srv.server_close()
        if tid is not None:
            db.delete_task(tid)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
