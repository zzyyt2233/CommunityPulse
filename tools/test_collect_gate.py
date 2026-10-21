# -*- coding: utf-8 -*-
"""离线验证：采集并发闸门（app/server.py 的 _CollectGate）。

覆盖：
- 同时最多 _COLLECT_MAX 个任务在跑，多的排队
- 排队期间任务状态是 pending（而不是还没开抓就先挂「进行中」）
- 释放一个槽位，排队的立刻顶上
- 进程重启回收会把 pending 一起标成中断
- 三条采集入口都挂了闸门（静态检查）
"""
import inspect
import io
import pathlib
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app import server  # noqa: E402
from app.core import db  # noqa: E402

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


def _fill(max_n):
    """占满所有槽位，返回持有者列表。"""
    held = []
    for _ in range(max_n):
        g = server._CollectGate()
        g.acquire()
        held.append(g)
    return held


def main():
    print(f"闸门上限 = {server._COLLECT_MAX}")
    created: list[int] = []
    held = []

    try:
        # --- 1. 超出上限要排队 ---
        held = _fill(server._COLLECT_MAX)
        extra = server._CollectGate()
        got = []
        th = threading.Thread(target=lambda: (extra.acquire(), got.append(1)))
        th.start()
        th.join(0.4)
        ok("超出上限的任务拿不到槽位（排队中）", not got)

        held[0].release()
        th.join(3)
        ok("有槽位空出来，排队的立刻顶上", bool(got))
        extra.release()
        for g in held[1:]:
            g.release()
        held = []

        # --- 2. 排队期间状态写的是 pending ---
        held = _fill(server._COLLECT_MAX)
        tid = db.create_task("闸门测试任务", "test", "", {}, status="pending")
        created.append(tid)
        g2 = server._CollectGate(tid)
        th2 = threading.Thread(target=g2.acquire)
        th2.start()
        time.sleep(0.4)
        ok("排队中的任务状态是 pending", db.get_task(tid)["status"] == "pending")

        held[0].release()
        th2.join(3)
        ok("拿到槽位后状态转 running", db.get_task(tid)["status"] == "running")
        info = db.get_task(tid)["message"] or ""
        ok("状态里写清了「开始采集」", "开始采集" in info)
        # g2 拿到槽位后必须还回去。它和第 1 步的 extra 不一样：extra 在第 1 步
        # 末尾就 release 了，g2 当年漏了这一句 —— 而 _COLLECT_SLOTS 是模块级
        # 全局量，泄漏一个就永久少一个。跑到第 5 步的端到端时可用槽位已经(3-1)=2
        # 个，于是「同时 3 个在跑」永远不成立；跑第二遍只剩 1 个，看起来像产品
        # 的闸门坏了，实际是测试自己在漏。
        g2.release()
        for g in held[1:]:
            g.release()
        held = []
        # 放回后应恢复满额：后面第 5 步要一次占满 _COLLECT_MAX 个槽。
        left = server._COLLECT_SLOTS._value
        ok(f"槽位已全部归还（可用数回到上限，实际剩 {left}）",
           left == server._COLLECT_MAX)

        # --- 3. 进程重启回收要覆盖 pending ---
        tid2 = db.create_task("重启回收测试", "test", "", {}, status="pending")
        created.append(tid2)
        db.reap_running_tasks("服务已重启（测试）")
        ok("重启回收把排队中的任务也标成中断",
           db.get_task(tid2)["status"] == "error")

        # --- 4. 三条采集入口都挂了闸门 ---
        ok("start_collect 走带闸门的线程目标",
           "_run_collect_gated" in inspect.getsource(server.start_collect))
        ok("recollect 走带闸门的线程目标",
           "_run_collect_gated" in inspect.getsource(server.recollect))
        ok("浏览器关键词采集过闸门",
           "_CollectGate" in inspect.getsource(server.start_browser_keyword_collect))
        ok("浏览器单链接采集过闸门",
           "_CollectGate" in inspect.getsource(server.start_browser_capture))
        src_all = (pathlib.Path(__file__).resolve().parents[1]
                   / "app" / "server.py").read_text(encoding="utf-8")
        ok("分析接口把 pending 也当作「采集中」",
           'in ("running", "pending")' in src_all)

        # --- 5. 端到端：真线程跑一轮，前几个在跑、多的排队 ---
        orig_route = server.route

        class _Slow:
            """占住槽位 1.2 秒的假采集器：不联网，只为把并发压出来。"""

            platform = "slow"
            label = "慢速测试源"

            def collect(self, url, limit, progress, settings, sort):
                time.sleep(1.2)

                class _R:
                    source_title = "慢速测试"
                    warnings: list = []
                    available = 0
                    complete = True
                    stop_reason = ""

                    def to_rows(self):
                        return []

                return _R()

        server.route = lambda u: _Slow()
        try:
            n = server._COLLECT_MAX + 1
            ids = [server.start_collect(f"闸门冒烟{i}", ["https://slow.example/x"],
                                        limit=10) for i in range(n)]
            created.extend(ids)
            # 从起线程到把 running 写进库有几十毫秒的抖动，机器忙的时候更容易晚。
            # 原来固定睡 0.5 秒就读状态，三次里有一次只看到 2 个 running ——
            # 那是测试在赌调度器，不是闸门少放了槽位。改成最多等 4 秒，
            # 一到目标状态就走；下一句「排队的随后也跑完」仍然会抓住真的卡死。
            deadline = time.time() + 4
            st = [db.get_task(i)["status"] for i in ids]
            while time.time() < deadline:
                time.sleep(0.05)
                st = [db.get_task(i)["status"] for i in ids]
                if (st.count("running") == server._COLLECT_MAX
                        and st.count("pending") == 1):
                    break
            ok(f"同时 {server._COLLECT_MAX} 个在跑、第 {n} 个排队",
               st.count("running") == server._COLLECT_MAX and st.count("pending") == 1)

            time.sleep(2.6)
            st2 = [db.get_task(i)["status"] for i in ids]
            ok("排队的随后也跑完（槽位没卡住）", all(s == "done" for s in st2))
        finally:
            server.route = orig_route
    finally:
        for g in held:
            try:
                g.release()
            except Exception:
                pass
        for tid in created:
            try:
                db.delete_task(tid)
            except Exception:
                pass

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
