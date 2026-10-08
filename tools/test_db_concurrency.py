# -*- coding: utf-8 -*-
"""离线验证：数据库并发写不互相踩。

背景（2026-10-04 实测）：库一度切成 WAL 模式，理由是「采集在写、分析在读」。
但本机环境下 WAL 库的并发写会大面积失败 —— 8 个线程同时建任务，只有 1 个成功，
其余全抛 `attempt to write a readonly database`；换成 DELETE（rollback journal）
是 8/8 成功。一个刚建的空库同样复现，所以跟库本身无关，是 WAL 依赖的 -shm
共享内存在本环境受限。

采集是批量写，分析是读为主，两者撞上的窗口本来就小；而 WAL 下任何一次并发写
失败都表现为「任务建不出来」，用户直接看得见。这笔买卖不划算，所以改回 rollback
journal。这个套件就是把当时的判断固定下来：如果有人为了「性能」再把 WAL 打开，
这里会红。

同时覆盖一条同样会踩的坑：journal_mode 是持久化在库文件里的属性，每连接执行
一次 PRAGMA journal_mode=... 会去抢排他锁，是并发失败的另一个来源。
"""
import io
import os
import re
import sqlite3
import sys
import threading
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.core import db  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def ok(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}" + (f"  {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ✗ 失败! {label}" + (f"  {detail}" if detail else ""))
    return bool(cond)


def concurrent_create(n):
    """起 n 个线程同时建任务，返回 (成功数, 错误集合)。"""
    ids, errs = [], []

    def mk(i):
        try:
            ids.append(db.create_task(f"并发写测试{i}", "test", "", {}))
        except Exception as e:                                  # noqa: BLE001
            errs.append(f"{type(e).__name__}: {e}")

    ts = [threading.Thread(target=mk, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    return ids, set(errs)


def main():
    print("== 库的 journal 模式 ==")
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    ok(f"不是 WAL（当前 {mode}）", str(mode).lower() != "wal",
       "WAL 在本环境并发写会大面积失败，见文件头注释")

    src = open(os.path.join(ROOT, "app", "core", "db.py"), encoding="utf-8").read()
    connect_src = src.split("def connect", 1)[1].split("def init_db", 1)[0]
    ok("connect() 里不再设置 journal_mode",
       "journal_mode" not in connect_src,
       "每连接设一次会抢排他锁，是并发失败的另一个来源")
    ok("connect() 仍然设置 busy_timeout",
       "busy_timeout" in connect_src, "写锁被占时要等，而不是立刻失败")
    init_src = src.split("def init_db", 1)[1].split("def create_task", 1)[0]
    ok("init_db() 里保留一次 journal_mode 设置",
       "journal_mode" in init_src, "它是持久化属性，切一次就够")

    print("\n== 并发建任务（真实线程）==")
    n = 8
    ids, errs = concurrent_create(n)
    try:
        ok(f"{n} 个线程同时建任务全部成功", len(ids) == n,
           f"成功 {len(ids)}/{n}  {sorted(errs) if errs else ''}")
        ok("没有出现「只读数据库」", not any("readonly" in e for e in errs),
           str(sorted(errs)))

        # 建出来的必须互不重复，否则主键/自增有问题
        ok("任务 id 不重复", len(set(ids)) == len(ids))
        statuses = {db.get_task(i)["status"] for i in ids}
        ok("建出来的任务都能读到", statuses == {"running"}, str(statuses))
    finally:
        for i in ids:
            try:
                db.delete_task(i)
            except Exception:                                   # noqa: BLE001
                pass

    print("\n== 并发读写混跑 ==")
    # 采集时一边写任务、一边有人在读列表，这是真实场景
    ids2, errs2, read_errs = [], [], []

    def writer(i):
        try:
            ids2.append(db.create_task(f"读写混跑{i}", "test", "", {}))
        except Exception as e:                                  # noqa: BLE001
            errs2.append(f"{type(e).__name__}: {e}")

    def reader():
        for _ in range(12):
            try:
                db.list_tasks()
                time.sleep(0.01)
            except Exception as e:                              # noqa: BLE001
                read_errs.append(f"{type(e).__name__}: {e}")

    ths = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    ths += [threading.Thread(target=reader) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=30)
    try:
        ok("写线程全部成功", len(ids2) == 5, f"成功 {len(ids2)}/5  {sorted(errs2) if errs2 else ''}")
        ok("读线程全程没报错", not read_errs, str(sorted(set(read_errs))[:2]))
    finally:
        for i in ids2:
            try:
                db.delete_task(i)
            except Exception:                                   # noqa: BLE001
                pass

    print("\n== 表结构没被这次改动影响 ==")
    with db.connect() as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("tasks", "comments", "analyses", "watchlist"):
        ok(f"{t} 表在", t in names)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
