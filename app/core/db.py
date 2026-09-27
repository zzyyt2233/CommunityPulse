"""SQLite 存储层：任务 / 评论 / 分析结果。"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .config import DB_PATH

# 单条评论正文上限。异常来源（抓取到的整页文本、误粘的长文、接口返回冗余字段）
# 会带出上万甚至上千万字符的内容：入库会让库文件急剧膨胀，分析时 jieba 逐条切词
# 会把整条链路拖死，前端渲染也会长时间白屏。正常评论远达不到这个长度。
MAX_CONTENT_LEN = 4000


def _trim(text: Any, limit: int = 200) -> str:
    """去掉首尾空白并限制长度，防止异常来源塞入超长字符串。"""
    s = str(text or "").strip()
    return s[:limit] if len(s) > limit else s


def _trim_content(text: Any) -> str:
    s = str(text or "").strip()
    if len(s) > MAX_CONTENT_LEN:
        return s[:MAX_CONTENT_LEN] + "…（原文过长，已截断）"
    return s

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    platform TEXT DEFAULT '',
    source_url TEXT DEFAULT '',
    source_title TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    params TEXT DEFAULT '{}',
    message TEXT DEFAULT '',
    total INTEGER DEFAULT 0,
    created_at REAL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    platform TEXT DEFAULT '',
    source_id TEXT DEFAULT '',
    comment_id TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    user_name TEXT DEFAULT '',
    content TEXT NOT NULL,
    like_count INTEGER DEFAULT 0,
    reply_count INTEGER DEFAULT 0,
    published_at REAL,
    collected_at REAL,
    extra TEXT DEFAULT '{}',
    url TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_comments_task ON comments(task_id);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    params TEXT DEFAULT '{}',
    result TEXT NOT NULL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_analyses_task ON analyses(task_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    platform TEXT DEFAULT '',
    interval_hours INTEGER DEFAULT 24,
    enabled INTEGER DEFAULT 1,
    last_run REAL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(_SCHEMA)


def create_task(name: str, platform: str, source_url: str, params: dict | None = None) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, platform, source_url, status, params, created_at)"
            " VALUES (?,?,?, 'running', ?, ?)",
            (name, platform, source_url, json.dumps(params or {}, ensure_ascii=False), time.time()),
        )
        return int(cur.lastrowid)


def update_task(task_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    with connect() as conn:
        conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", vals)


def get_task(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None


def reap_running_tasks(message: str = "") -> int:
    """把所有仍为 running 的任务标记为「已中断」，返回处理条数。

    采集跑在内存线程里，进程一退出线程就没了，但任务状态会永远停在「进行中」：
    列表一直显示进行中，点「分析」还会被误判成「采集尚未完成」弹二次确认。
    由启动流程在确认没有其他实例在跑时调用。
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE tasks SET status='error', finished_at=?, message=?"
            " WHERE status='running'",
            (time.time(), message or
             "服务已重启，本次采集中断；点「分析」可查看已入库的数据"),
        )
        return cur.rowcount


def maybe_vacuum(threshold: float = 0.30) -> tuple[int, int] | None:
    """空闲页占比过高时压缩数据库，返回 (压缩前字节, 压缩后字节)，没达到阈值则返回 None。

    SQLite 删除记录只把页面挂进 freelist，不会把空间还给操作系统：反复删任务
    会让库文件只增不减（实测删到 73% 空闲页，文件却仍是 14MB）。这里在空闲比
    例超过阈值时做一次 VACUUM，让文件真正缩回去。

    必须在没有其他连接持有写锁时调用——启动流程里调用最安全。
    """
    try:
        before = os.path.getsize(DB_PATH)
    except OSError:
        return None
    with connect() as conn:
        total = int(conn.execute("PRAGMA page_count").fetchone()[0] or 0)
        free = int(conn.execute("PRAGMA freelist_count").fetchone()[0] or 0)
    if not total or before < 1024 * 1024 or free / total < threshold:
        return None
    with sqlite3.connect(DB_PATH, timeout=60) as conn:
        conn.isolation_level = None      # VACUUM 不能在事务里执行
        conn.execute("VACUUM")
    try:
        after = os.path.getsize(DB_PATH)
    except OSError:
        after = before
    return (before, after)


def list_tasks(limit: int = 100) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except Exception:
            d["params"] = {}
        out.append(d)
    return out


def delete_task(task_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM comments WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM analyses WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def insert_comments(task_id: int, rows: list[dict]) -> int:
    """批量写入评论，按 (task_id, comment_id) 去重；返回实际新增条数。"""
    if not rows:
        return 0
    with connect() as conn:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT comment_id FROM comments WHERE task_id=? AND comment_id!=''", (task_id,)
            )
        }
        payload = []
        seen = set()
        for r in rows:
            cid = str(r.get("comment_id") or "")
            if cid:
                if cid in existing or cid in seen:
                    continue
                seen.add(cid)
            payload.append(
                (
                    task_id,
                    r.get("platform", ""),
                    r.get("source_id", ""),
                    cid,
                    r.get("parent_id", ""),
                    _trim(r.get("user_name", "")),
                    _trim_content(r.get("content")),
                    int(r.get("like_count") or 0),
                    int(r.get("reply_count") or 0),
                    r.get("published_at"),
                    time.time(),
                    json.dumps(r.get("extra") or {}, ensure_ascii=False),
                    _trim(r.get("url", "")),
                )
            )
        if not payload:
            return 0
        conn.executemany(
            "INSERT INTO comments (task_id, platform, source_id, comment_id, parent_id,"
            " user_name, content, like_count, reply_count, published_at, collected_at, extra, url)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload,
        )
        conn.execute(
            "UPDATE tasks SET total=(SELECT COUNT(*) FROM comments WHERE task_id=?) WHERE id=?",
            (task_id, task_id),
        )
        return len(payload)


def get_comments(task_id: int, limit: int = 200000) -> list[dict]:
    """读取任务评论（按点赞降序）。

    limit 只是防御性上限，不要用来做「只取前 N 条」的业务控制：
    早期默认 50000，超过后会被静默截断——导出明细、按评论全量分析时
    数据悄悄变少且不报错，是最难发现的一类问题。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM comments WHERE task_id=? ORDER BY like_count DESC, id LIMIT ?",
            (task_id, limit),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except Exception:
            d["extra"] = {}
        out.append(d)
    return out


def count_comments(task_id: int) -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM comments WHERE task_id=?", (task_id,)).fetchone()[0])


def save_analysis(task_id: int, params: dict, result: dict, keep: int = 3) -> int:
    """保存一次分析结果，同一任务只保留最近 keep 条。

    分析结果 JSON 单条可达 100KB+，早期实现只插不删：反复「重新分析」
    会让 analyses 表无限增长，而读侧始终只取最新一条，历史记录毫无用处，
    却白白把库文件撑大（实测 27 条就占 1.3MB）。这里顺手清理掉更旧的。
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO analyses (task_id, params, result, created_at) VALUES (?,?,?,?)",
            (task_id, json.dumps(params or {}, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False), time.time()),
        )
        new_id = int(cur.lastrowid)
        conn.execute(
            "DELETE FROM analyses WHERE task_id=? AND id NOT IN ("
            "  SELECT id FROM analyses WHERE task_id=? ORDER BY id DESC LIMIT ?)",
            (task_id, task_id, int(keep)),
        )
        return new_id


def get_latest_analysis(task_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["result"] = json.loads(d["result"])
            d["params"] = json.loads(d.get("params") or "{}")
        except Exception:
            pass
        return d


def merge_task(target_task_id: int, source_task_ids: list[int]) -> int:
    """把若干任务的评论并入目标任务（用于合并多链接对比）。"""
    total = 0
    with connect() as conn:
        for sid in source_task_ids:
            rows = conn.execute("SELECT * FROM comments WHERE task_id=?", (sid,)).fetchall()
            have = {
                r[0] for r in conn.execute(
                    "SELECT comment_id FROM comments WHERE task_id=? AND comment_id!=''",
                    (target_task_id,),
                )
            }
            payload = []
            for r in rows:
                if r["comment_id"] and r["comment_id"] in have:
                    continue
                payload.append(
                    (target_task_id, r["platform"], r["source_id"], r["comment_id"], r["parent_id"],
                     r["user_name"], r["content"], r["like_count"], r["reply_count"], r["published_at"],
                     time.time(), r["extra"], r["url"])
                )
            if payload:
                conn.executemany(
                    "INSERT INTO comments (task_id, platform, source_id, comment_id, parent_id,"
                    " user_name, content, like_count, reply_count, published_at, collected_at, extra, url)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", payload)
                total += len(payload)
        conn.execute(
            "UPDATE tasks SET total=(SELECT COUNT(*) FROM comments WHERE task_id=?) WHERE id=?",
            (target_task_id, target_task_id),
        )
    return total


# ---- 监测清单 ----
def list_watchlist() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY id DESC").fetchall()]


def add_watch(name: str, url: str, platform: str = "", interval_hours: int = 24) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO watchlist (name, url, platform, interval_hours, enabled, last_run)"
            " VALUES (?,?,?,?,1,NULL)", (name, url, platform, interval_hours))
        return int(cur.lastrowid)


def remove_watch(wid: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE id=?", (wid,))
