"""HTTP 端点全量巡检：把服务端的每个路由都真实打一遍，确认无 5xx / 无异常字段。"""
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8801"
PORT = 8801
FAIL = []


def hit(method, path, body=None, raw=False):
    req = urllib.request.Request(BASE + path, method=method)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=90) as r:
            txt = r.read()
            return r.status, (txt if raw else json.loads(txt.decode("utf-8", "ignore")))
    except urllib.error.HTTPError as e:
        txt = e.read()
        try:
            return e.code, json.loads(txt.decode("utf-8", "ignore"))
        except Exception:
            return e.code, txt[:200]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def hit_raw_post(path, payload: bytes):
    """发送原始字节作为 JSON 体，用于验证非 dict / 非法 JSON 的处理。"""
    req = urllib.request.Request(BASE + path, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:120]
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def expect(label, cond, info=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {info}" if info else ""))
    if not cond:
        FAIL.append(label)


TASK = 15  # 原神，7.0 相关舆情：2700 条、已分析过

s, j0 = hit("GET", "/api/tasks")
BASE_IDS = {t["id"] for t in (j0.get("items") or [])}
print(f"巡检基线：{len(BASE_IDS)} 个任务\n")

print("== GET 端点 ==")
s, j = hit("GET", "/api/health")
expect("GET /api/health", s == 200 and j.get("ok") is True)

s, j = hit("GET", "/api/platforms")
expect("GET /api/platforms", s == 200 and j.get("ok") and len(j["items"]) > 0,
       f"{len(j.get('items') or [])} 个采集器")

s, j = hit("GET", "/api/credentials")
expect("GET /api/credentials", s == 200 and j.get("ok") and "items" in j,
       f"browser={'on' if j.get('browser') else 'off'}")

s, j = hit("GET", "/api/tasks")
expect("GET /api/tasks", s == 200 and j.get("ok") and isinstance(j.get("items"), list),
       f"{len(j.get('items') or [])} 个任务")

s, j = hit("GET", f"/api/tasks/{TASK}")
t = (j or {}).get("task") or {}
expect(f"GET /api/tasks/{TASK}", s == 200 and t.get("id") == TASK,
       f"count={t.get('count')} status={t.get('status')}")

s, j = hit("GET", f"/api/analysis/{TASK}")
a = (j or {}).get("analysis")
expect(f"GET /api/analysis/{TASK}", s == 200 and j.get("ok") and a,
       f"total={(a or {}).get('result', {}).get('stats', {}).get('total')}")

s, j = hit("GET", "/api/settings")
expect("GET /api/settings", s == 200 and j.get("ok") is True)

s, j = hit("GET", "/api/sentiment-memory")
expect("GET /api/sentiment-memory", s == 200 and j.get("ok") and "memory" in j,
       f"{len(j.get('memory') or {})} 个词条标签")

s, j = hit("GET", "/api/comment-memory")
expect("GET /api/comment-memory", s == 200 and j.get("ok") and "items" in j,
       f"{j.get('count')} 条语句标注")

s, j = hit("GET", f"/api/comments/{TASK}?limit=5")
expect(f"GET /api/comments/{TASK}", s == 200 and j.get("ok") and len(j.get("items", [])) == 5,
       f"total={j.get('total')} returned={j.get('returned')}")
if j.get("items"):
    it = j["items"][0]
    expect("评论条目字段齐全（含记忆标签与情感）",
           all(k in it for k in ("content", "key", "sentiment", "sent_score")))

print("\n== 静态资源 ==")
s, body = hit("GET", "/", raw=True)
expect("GET / 返回页面", s == 200 and b"CommunityPulse" in body, f"{len(body)} B")
s, body = hit("GET", "/app.js", raw=True)
expect("GET /app.js 且含本次改动",
       s == 200 and b"watchBrowserKeyword" in body and b"task_id" in body,
       f"{len(body)} B")
s, body = hit("GET", "/style.css", raw=True)
expect("GET /style.css", s == 200, f"{len(body)} B")

print("\n== 错误分支 ==")
s, j = hit("GET", "/api/tasks/999999")
expect("不存在任务返回 404", s == 404)
s, j = hit("GET", "/api/nope")
expect("未知接口返回 404", s == 404)
s, j = hit("POST", "/api/import", {"text": "", "fmt": "text"})
expect("空内容导入被拒", j.get("ok") is False)

print("\n== 参数校验（都在发网络请求前返回）==")
s, j = hit("POST", "/api/tasks", {})
expect("建任务缺链接被拒", j.get("ok") is False)
s, j = hit("POST", "/api/discover", {})
expect("发现缺关键词被拒", j.get("ok") is False)
s, j = hit("POST", "/api/discover", {"keyword": "x", "platform": "不存在"})
expect("发现不支持的平台被拒", j.get("ok") is False)
s, j = hit("POST", "/api/keyword-collect", {})
expect("关键词采集缺关键词被拒", j.get("ok") is False)
s, j = hit("POST", "/api/keyword-collect", {"keyword": "x", "platform": "不存在"})
expect("关键词采集不支持的平台被拒", j.get("ok") is False)
s, j = hit("POST", "/api/discover", {"keyword": "x", "platform": "taptap"})
expect("浏览器搜索平台给出改用「搜索并采集」的指引",
       j.get("ok") is False and "浏览器" in (j.get("error") or ""),
       (j.get("error") or "")[:34])
s, j = hit("POST", "/api/preview", {})
expect("试抓缺链接被拒", j.get("ok") is False)
s, j = hit("POST", "/api/analyze/999999", {})
expect("分析不存在的任务 -> 404", s == 404)
s, j = hit("POST", "/api/tasks/999999/recollect", {})
expect("重采不存在的任务被拒", j.get("ok") is False, (j.get("error") or "")[:30])
s, j = hit("GET", "/api/export/999999?fmt=csv")
expect("导出不存在的任务 -> 404", s == 404)

# 未登录平台的「先登录」提示（所有平台当前都未接入，故该分支可达且不发网络请求）
s, j = hit("POST", "/api/keyword-collect", {"keyword": "原神", "platform": "weibo"})
expect("未登录平台提示先登录", j.get("ok") is False and "登录" in (j.get("error") or ""),
       (j.get("error") or "")[:34])
s, j = hit("POST", "/api/discover", {"keyword": "原神", "platform": "nga"})
expect("发现未登录平台提示先登录", j.get("ok") is False and "登录" in (j.get("error") or ""),
       (j.get("error") or "")[:34])

print("\n== POST 导出三格式 ==")
for fmt, sig in (("xlsx", b"PK"), ("csv", None), ("html", b"<")):
    s, j = hit("GET", f"/api/export/{TASK}?fmt={fmt}")
    ok = s == 200 and j.get("ok") and j.get("url")
    expect(f"导出 {fmt}", ok, f"{j.get('name')}")
    if ok:
        # 服务端返回的 url 已是 percent-encoded 的（ früher 是裸中文，浏览器
        # href 会帮忙转码但 fetch/地址栏不会）。这里验两种用法都拿得到文件：
        #   1) 直接用（对应 <a href> 与 JS fetch）
        #   2) 再编码一次（对应某些客户端二次编码）——服务端 unquote 只解一层，
        #      二次编码必然 404，所以这里只要求「直接用」成功。
        s2, body = hit("GET", j["url"], raw=True)
        good = s2 == 200 and isinstance(body, bytes) and len(body) > 500 \
            and (sig is None or body.startswith(sig))
        expect(f"下载 {fmt}（中文名）", good, f"{len(body) if isinstance(body, bytes) else body} B")
        # 裸中文路径也仍能容错（说明服务端做了 unquote）
        is_enc = j["url"].replace("/exports/", "").startswith("%")
        expect(f"导出 URL 已百分号编码（{fmt}）", is_enc)

print("\n== POST 重新分析 ==")
s, j = hit("POST", f"/api/analyze/{TASK}", {})
st = ((j or {}).get("result") or {}).get("stats") or {}
expect(f"分析 #{TASK}", s == 200 and j.get("ok") is True,
       f"total={st.get('total')} raw={st.get('raw_total')} cut={st.get('cut')}")
expect("分析结果带任务总条数（口径提示用）", "task_total" in st, f"task_total={st.get('task_total')}")

print("\n== POST 合并任务（merge 端点）==")
s, j1 = hit("POST", "/api/import", {"text": json.dumps({
    "platform": "douyin", "source_url": "u1", "source_title": "巡检1", "count": 1,
    "comments": [{"comment_id": "p1", "content": "巡检评论一"}]}, ensure_ascii=False),
    "fmt": "json"})
s, j2 = hit("POST", "/api/import", {"text": json.dumps({
    "platform": "douyin", "source_url": "u2", "source_title": "巡检2", "count": 1,
    "comments": [{"comment_id": "p2", "content": "巡检评论二"}]}, ensure_ascii=False),
    "fmt": "json"})
s, jm = hit("POST", "/api/merge", {"task_ids": [j1.get("task_id"), j2.get("task_id")],
                                   "name": "巡检合并"})
expect("合并两个任务", s == 200 and jm.get("ok") is True and jm.get("count") == 2,
       f"新任务 #{jm.get('task_id')} 共 {jm.get('count')} 条")
s, jd = hit("GET", f"/api/tasks/{jm.get('task_id')}")
expect("合并结果可读", ((jd or {}).get("task") or {}).get("count") == 2)

print("\n== 健壮性（畸形请求 / 超长内容 / 路径穿越）==")
# 一、非 dict 的 JSON 请求体：早期实现直接 .get() 会抛 AttributeError -> 500
for label, payload in (("数组", b"[1,2]"), ("字符串", b'"x"'), ("null", b"null"),
                       ("非法 JSON", b"{bad")):
    s, j = hit_raw_post("/api/import", payload)
    expect(f"请求体为 JSON {label} 时返回 400 而非 500", s == 400, f"HTTP {s}")

# 二、超长正文必须被截断，否则会撑爆库并让分析卡死
huge = "A" * (2 * 1024 * 1024)
s, jh = hit("POST", "/api/import", {"text": json.dumps({
    "platform": "manual", "count": 1,
    "comments": [{"comment_id": "huge1", "content": huge}]}, ensure_ascii=False),
    "fmt": "json"})
expect("超长正文仍能入库", s == 200 and jh.get("ok") is True)
SIZES = {}
if jh.get("task_id"):
    from app.core.config import DB_PATH  # noqa: E402
    import sqlite3  # noqa: E402
    _c = sqlite3.connect(DB_PATH)
    n = _c.execute("SELECT MAX(LENGTH(content)) FROM comments WHERE task_id=?",
                   (jh["task_id"],)).fetchone()[0]
    expect("超长正文已被截断（<=4100 字符）", n is not None and n <= 4100, f"库中 {n} 字符")
    # 连续多次分析，历史分析结果只应保留最近几条
    for _ in range(5):
        hit("POST", f"/api/analyze/{jh['task_id']}", {})
    kept = _c.execute("SELECT COUNT(*) FROM analyses WHERE task_id=?",
                      (jh["task_id"],)).fetchone()[0]
    expect("历史分析结果只保留最近 3 条", kept <= 3, f"保留 {kept} 条")
    _c.close()
    hit("DELETE", f"/api/tasks/{jh['task_id']}")

# 三、静态目录不可穿越，且不允许「前缀相同的兄弟目录」绕过
import socket  # noqa: E402
for label, p in (("上级目录", "/../config.local.yaml"), ("源码", "/../app/server.py"),
                 ("兄弟目录", "/../web/vendor/x.txt"), ("绝对路径", "/C:/Windows/win.ini")):
    try:
        sk = socket.create_connection(("127.0.0.1", PORT), timeout=10)
        sk.sendall(("GET %s HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n" % p).encode())
        buf = b""
        while True:
            chunk = sk.recv(65536)
            if not chunk:
                break
            buf += chunk
        sk.close()
        code = int(buf.split(b"\r\n")[0].split()[1])
    except Exception as exc:  # noqa: BLE001
        code = -1
    expect(f"静态路径穿越被拦 — {label}", code == 404, f"HTTP {code}")

print("\n== 存储层（SQLite 碎片回收）==")
from app.core import db as _db  # noqa: E402
try:
    _before = _db.maybe_vacuum(threshold=0.0)  # 强制跑一次，验证不报错
    expect("maybe_vacuum 可正常执行且不损坏数据", True,
           ("已回收 %.1f→%.1f MB" % (_before[0] / 1024 / 1024, _before[1] / 1024 / 1024))
           if _before else "无碎片，未触发（正常）")
except Exception as _e:  # noqa: BLE001
    expect("maybe_vacuum 可正常执行且不损坏数据", False, f"{type(_e).__name__}: {_e}")
expect("回收后任务仍完整可读", hit("GET", "/api/tasks")[0] == 200)

print("\n== 清理巡检数据 ==")
for tid in (j1.get("task_id"), j2.get("task_id"), jm.get("task_id")):
    if tid:
        s, j = hit("DELETE", f"/api/tasks/{tid}")
        expect(f"删除巡检任务 #{tid}", s == 200 and j.get("ok") is True)

print("\n== 任务数不泄漏（接口不应偷偷留下任务）==")
s, jn = hit("GET", "/api/tasks")
LEFT = {t["id"] for t in (jn.get("items") or [])} - BASE_IDS
expect("巡检结束后没有多余任务残留", not LEFT, f"残留 {sorted(LEFT)}" if LEFT else "无残留")
for tid in sorted(LEFT):          # 兜底清掉，保持环境干净
    hit("DELETE", f"/api/tasks/{tid}")

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ -> {FAIL}"))
