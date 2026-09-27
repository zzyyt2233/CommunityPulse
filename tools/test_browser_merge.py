"""浏览器关键词采集「多来源合并为一个任务」离线单测。

覆盖两层：
  A. /api/import 的追加语义（task_id 存在 → 并入该任务而不是新建）
  B. start_browser_keyword_collect 的编排（搜索 N 条 → 全部写进同一个任务）

用假浏览器替代真实 CDP：capture_via_browser 被替换成「把若干评论写进 task_id」，
模拟浏览器脚本回传 /api/import 后的效果。不依赖网络与登录态。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import db                       # noqa: E402
from app.core import browser_cdp               # noqa: E402
from app import server                          # noqa: E402

FAIL = []


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAIL.append(label)


db.init_db()

# ---------------- A. /api/import 追加语义 ----------------
print("== A. /api/import 追加语义 ==")


def import_call(body: dict) -> dict:
    """直接调用 Handler._api_post 的 import 分支（跳过 HTTP 层，只验业务语义）。"""
    out = {}
    h = server.Handler.__new__(server.Handler)
    h._json = lambda obj, code=200: out.update(obj)
    h._api_post("/api/import", body)
    return out


payload = {
    "platform": "douyin", "source_url": "https://www.douyin.com/video/111",
    "source_title": "视频一", "count": 2,
    "comments": [
        {"comment_id": "c1", "user_name": "甲", "content": "好", "like_count": 1},
        {"comment_id": "c2", "user_name": "乙", "content": "差", "like_count": 2},
    ],
}
r1 = import_call({"text": json.dumps(payload, ensure_ascii=False), "fmt": "json"})
check("A1 首次回传新建任务", r1.get("ok") and r1.get("count") == 2 and not r1.get("appended"))
tid = r1["task_id"]

payload2 = dict(payload, source_url="https://www.douyin.com/video/222",
                source_title="视频二", comments=[
                    {"comment_id": "c3", "user_name": "丙", "content": "一般", "like_count": 0},
                    {"comment_id": "c2", "user_name": "乙", "content": "差", "like_count": 2},
                ])
r2 = import_call({"text": json.dumps(payload2, ensure_ascii=False), "fmt": "json",
                  "task_id": tid})
check("A2 带 task_id 时并入同一任务", r2.get("ok") and r2.get("task_id") == tid
      and r2.get("appended") is True)
check("A3 去重后只新增 1 条（c3）", r2.get("count") == 1)
check("A4 累计条数 = 3", r2.get("total") == 3)
check("A5 DB 里确实只有 1 个任务包含这 3 条", db.count_comments(tid) == 3)

r3 = import_call({"text": json.dumps(payload2, ensure_ascii=False), "fmt": "json",
                  "task_id": 999999})
check("A6 不存在的 task_id 回退为新建任务", r3.get("ok") and r3.get("task_id") != 999999)

for t in (tid, r3.get("task_id")):
    db.delete_task(t)

# ---------------- B. start_browser_keyword_collect 编排 ----------------
print("\n== B. 浏览器关键词采集合并为一个任务 ==")

URLS = [f"https://www.douyin.com/video/{i}" for i in (10, 20, 30)]

browser_cdp.search_via_browser = lambda *a, **k: {"ok": True, "urls": list(URLS)}
seen_task_ids = []


def fake_capture(url, platform, target=500, base_url=None, timeout=300,
                 script_text=None, task_id=None):
    """模拟浏览器回传：把 3 条评论写进服务端指定的任务。"""
    seen_task_ids.append(task_id)
    n = url.rsplit("/", 1)[1]
    rows = [{"comment_id": f"{n}-{i}", "user_name": "u", "content": f"评论{i}",
             "like_count": i, "platform": platform} for i in range(3)]
    added = db.insert_comments(task_id, rows)
    return {"ok": True, "count": len(rows), "task_id": task_id,
            "total": db.count_comments(task_id), "added": added}


browser_cdp.capture_via_browser = fake_capture
server.browser_cdp = browser_cdp

job_id = server.start_browser_keyword_collect("douyin", "原神", 3, 50, "hot", label="抖音")
job = server._CAPTURE_JOBS[job_id]
for _ in range(100):
    if job["status"] == "done":
        break
    time.sleep(0.05)

res = job.get("result") or {}
check("B1 采集完成", job["status"] == "done" and res.get("ok") is True)
check("B2 只创建 1 个任务（而不是每视频一个）", res.get("task_ids") == [res.get("task_id")])
check("B3 每个来源都追加到同一 task_id", seen_task_ids == [res.get("task_id")] * 3)
check("B4 累计条数 = 9（3 来源 × 3 条）", res.get("total") == 9)
check("B5 job 暴露 task_id 供前端轮询显示进度", job.get("task_id") == res.get("task_id"))

task = db.get_task(res["task_id"])
params = task.get("params")
params = json.loads(params) if isinstance(params, str) else (params or {})
check("B6 任务状态为 done", task.get("status") == "done")
check("B7 任务 total 与 DB 一致", task.get("total") == db.count_comments(res["task_id"]) == 9)
check("B8 params.sources 记录 3 个来源", len(params.get("sources") or []) == 3)
check("B9 params 标记 browser=True", params.get("browser") is True)
check("B10 分析口径：per 50 × 3 来源 = 目标 150",
      params.get("limit") == 50 and len(params.get("sources") or []) == 3)

# 重采：浏览器任务应给出明确提示，而不是误跑服务端直连
ok, err = server.recollect(res["task_id"], 50, "hot")
check("B11 浏览器任务重采被明确拦下", ok is False and "搜索并采集" in err)

# 轮询接口要把 task_id 带出去，前端才能显示实时进度
polled = {}
h = server.Handler.__new__(server.Handler)
h._json = lambda obj, code=200: polled.update(obj)
h._api_get(f"/api/browser-keyword-collect/{job_id}", {})
check("B12 轮询接口回传 task_id", polled.get("task_id") == res.get("task_id"))

db.delete_task(res["task_id"])

# ---------------- C. 启动收尾：僵尸 running 任务 / 内存 job 清理 ----------------
print("\n== C. 启动收尾与内存清理 ==")

import os      # noqa: E402
import subprocess  # noqa: E402

before_running = [t["id"] for t in db.list_tasks() if t["status"] == "running"]
a = db.create_task("僵尸任务A", "bilibili", "", {})
b = db.create_task("僵尸任务B", "bilibili", "", {})
done_task = db.create_task("正常的已完成任务", "bilibili", "", {})
db.update_task(done_task, status="done", finished_at=time.time(), total=0, message="完成")

n = db.reap_running_tasks()
check("C1 中断清理覆盖所有 running 任务", n >= 2 + len(before_running))
check("C2 僵尸任务被标记为 error 且带中断说明",
      db.get_task(a)["status"] == "error" and "中断" in (db.get_task(a)["message"] or ""))
check("C3 已完成任务的 total/status 不被改动",
      db.get_task(done_task)["status"] == "done")

for tid_mark in before_running:
    t = db.get_task(tid_mark)
    check(f"C4 历史遗留 running 任务 #{tid_mark} 已收尾 "
          f"({t['status']} / DB {db.count_comments(tid_mark)} 条保留)",
          t["status"] == "error" and db.count_comments(tid_mark) >= 0)

# 另一个实例仍在跑时，不得误伤它正在执行的任务
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
try:
    server.INSTANCE_FILE.write_text(
        json.dumps({"pid": child.pid, "port": 9999}), encoding="utf-8")
    alive = db.create_task("别的实例正在采集", "bilibili", "", {})
    server._reap_orphans(8801)
    check("C5 检测到其他实例在跑时不误伤 running 任务",
          db.get_task(alive)["status"] == "running")
    check("C6 实例文件已更新为本进程",
          json.loads(server.INSTANCE_FILE.read_text(encoding="utf-8"))["pid"] == os.getpid())
    db.delete_task(alive)
finally:
    child.terminate()

# 假 pid → 走正常清理分支
server.INSTANCE_FILE.write_text(json.dumps({"pid": 999999, "port": 8801}),
                                encoding="utf-8")
leftover = db.create_task("残留任务", "bilibili", "", {})
server._reap_orphans(8801)
check("C7 没有其他实例时正常收尾残留任务",
      db.get_task(leftover)["status"] == "error")
db.delete_task(leftover)
server.INSTANCE_FILE.unlink(missing_ok=True)

# 内存 job 表只增不减 → 过期后要能自动清掉
now = time.time()
server._CAPTURE_JOBS.clear()
server._CAPTURE_JOBS["old_done"] = {"status": "done", "created": now - 3 * 3600}
server._CAPTURE_JOBS["new_done"] = {"status": "done", "created": now}
server._CAPTURE_JOBS["old_running"] = {"status": "running", "created": now - 3 * 3600}
server._prune_jobs()
check("C8 过期的已完成 job 被回收",
      "old_done" not in server._CAPTURE_JOBS and "new_done" in server._CAPTURE_JOBS)
check("C9 仍在跑的 job 不会被误清", "old_running" in server._CAPTURE_JOBS)
server._CAPTURE_JOBS.clear()

for t in (a, b, done_task):
    db.delete_task(t)

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ -> {FAIL}"))
sys.exit(1 if FAIL else 0)
