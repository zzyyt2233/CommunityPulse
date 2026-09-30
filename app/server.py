"""本地 Web 服务：标准库实现，零额外依赖。

启动后浏览器访问 http://127.0.0.1:8766
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from .core import db
from .analysis import HAS_JIEBA, analyze
from .analysis import comment_memory, sentiment_memory
from .analysis.sentiment import score_text
from .collectors import all_collectors, parse_import, route
from .collectors.discovery import PLATFORMS as DISCOVERY_PLATFORMS, search
from .core import browser_cdp
from .core.config import DATA_DIR, HOST, PORT, WEB_DIR, load_settings, save_settings
from .core.credentials import ACCESS, BY_KEY, status_of, cookie_for
from .export import EXPORT_DIR, to_csv, to_excel, to_html_report

mimetypes.add_type("application/javascript", ".js")

# 实际绑定的端口（端口被占用时会顺延，serve() 里写回这里，供浏览器采集回传地址使用）
APP_PORT = PORT

# 实例标记：用于判断「是否还有另一个进程在用同一个数据库」。
# 数据库是共享文件，若有另一个实例在跑，它可能正有采集任务在执行，
# 这时绝不能把它那些 running 任务当成残留给中断掉。
INSTANCE_FILE = DATA_DIR / "instance.json"


# ---------------- 登录态自动捕获（点「打开登录页」后后台轮询） ----------------
# 用户在调试浏览器里登录后，后台线程每几秒读一次 Cookie，一旦检测到登录标记就写回
# settings.json，无需再手动点「一键获取」。浏览器关掉或超时则结束本次等待。
_CAPTURE_LOCK = threading.Lock()
_CAPTURE_WATCHERS: dict[str, dict] = {}  # platform -> {running, done, error, result, started}

# 浏览器一键采集任务（抖音 / 小红书）：job_id -> {status, result, error, created}
_CAPTURE_JOBS: dict[str, dict] = {}

# 已完成的浏览器采集 job 在内存里保留多久：够前端轮询取到结果，又不至于只增不减
_JOB_TTL = 2 * 3600


def _prune_jobs() -> None:
    """清掉早已完成、前端不可能再轮询的 job（这些状态只存在内存里）。"""
    now = time.time()
    for jid, job in list(_CAPTURE_JOBS.items()):
        if job.get("status") == "done" and now - (job.get("created") or now) > _JOB_TTL:
            _CAPTURE_JOBS.pop(jid, None)


def _read_instance() -> dict:
    try:
        data = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否存活（Windows 上 os.kill(pid, 0) 只做句柄探测，不会结束进程）。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _reap_orphans(port: int) -> None:
    """启动时收尾：把上一次进程遗留的 running 任务标记为「已中断」。

    若检测到另一个实例仍在运行（共享同一个 pulse.db），则跳过清理并提示，
    避免把对方正在执行的采集任务误判成残留。
    """
    inst = _read_instance()
    other = int(inst.get("pid") or 0)
    if other and other != os.getpid() and _pid_alive(other):
        print(f"  [提示] 检测到另一个实例仍在运行（PID {other}，端口 {inst.get('port')}），"
              "已跳过「中断任务」清理；两个实例共用同一数据目录，建议只保留一个。")
    else:
        n = db.reap_running_tasks()
        if n:
            print(f"  已把上次遗留的 {n} 个「进行中」任务标记为中断")
    try:
        INSTANCE_FILE.write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "started": time.time()},
            ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def start_browser_capture(platform: str, url: str, target: int) -> str:
    """后台启动浏览器采集，返回 job_id；前端轮询 /api/browser-capture/<id> 取结果。"""
    _prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    _CAPTURE_JOBS[job_id] = {"status": "pending", "result": None, "error": None,
                             "created": time.time()}
    base = f"http://{HOST}:{APP_PORT}/"

    def _run() -> None:
        _CAPTURE_JOBS[job_id]["status"] = "running"
        try:
            r = browser_cdp.capture_via_browser(url, platform, target=target,
                                                base_url=base, timeout=300)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        _CAPTURE_JOBS[job_id]["status"] = "done"
        _CAPTURE_JOBS[job_id]["result"] = r

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def start_browser_keyword_collect(platform: str, keyword: str, top_n: int,
                                  comment_limit: int, sort: str = "hot",
                                  label: str = "") -> str:
    """抖音 / 小红书 / TapTap：浏览器里搜索关键词 → 取前 N 条链接 → 逐条浏览器抓评论。

    所有来源的评论**写入同一个任务**，与服务端直连路径（_run_collect_multi）口径一致，
    这样「有效评论」才会随视频数累加。返回 job_id 供前端轮询；
    任务 id 通过 job["task_id"] 与 result.task_id 暴露。
    """
    _prune_jobs()
    job_id = uuid.uuid4().hex[:12]
    _CAPTURE_JOBS[job_id] = {"status": "pending", "result": None, "error": None,
                             "task_id": None, "created": time.time()}
    base = f"http://{HOST}:{APP_PORT}/"

    def _fail(task_id: int | None, msg: str, **extra) -> None:
        if task_id:
            db.update_task(task_id, status="error", finished_at=time.time(), message=msg)
        _CAPTURE_JOBS[job_id]["status"] = "done"
        _CAPTURE_JOBS[job_id]["error"] = msg
        _CAPTURE_JOBS[job_id]["result"] = dict(
            {"ok": False, "error": msg, "keyword": keyword, "platform": platform,
             "task_id": task_id}, **extra)

    def _run() -> None:
        _CAPTURE_JOBS[job_id]["status"] = "running"
        # 先建任务：搜索一返回就把它当作唯一容器，逐个来源往里追加评论
        task_id = db.create_task(
            f"{keyword} · {label or platform}热度前{top_n}", platform, "",
            {"limit": comment_limit, "sort": sort, "browser": True, "keyword": keyword,
             "sources": [], "source_count": 0})
        _CAPTURE_JOBS[job_id]["task_id"] = task_id
        db.update_task(task_id, message=f"正在浏览器中搜索「{keyword}」…")

        try:
            r = browser_cdp.search_via_browser(keyword, platform, top_n=top_n,
                                               base_url=base, timeout=180)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not r.get("ok"):
            _fail(task_id, r.get("error") or "搜索失败")
            return

        urls = [u for u in (r.get("urls") or []) if u]
        if not urls:
            _fail(task_id, f"没有在浏览器里搜到「{keyword}」的相关来源")
            return

        # 来源写进任务参数：前端展示、「重新采集」复用、分析口径（每来源上限 × 来源数）
        # browser=True 标记该任务走浏览器助手，recollect 会据此给出明确提示
        db.update_task(task_id, source_url=urls[0], params=json.dumps({
            "limit": comment_limit, "sort": sort, "browser": True, "keyword": keyword,
            "sources": urls, "source_count": len(urls),
        }, ensure_ascii=False))

        fails: list[str] = []
        for idx, u in enumerate(urls, 1):
            db.update_task(task_id, message=f"[{idx}/{len(urls)}] 正在浏览器抓取评论…")
            try:
                cr = browser_cdp.capture_via_browser(
                    u, platform, target=comment_limit, base_url=base,
                    timeout=240, task_id=task_id)
            except Exception as exc:  # noqa: BLE001
                cr = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            got = db.count_comments(task_id)
            if cr.get("ok"):
                db.update_task(task_id, total=got, message=(
                    f"[{idx}/{len(urls)}] 浏览器实得 {cr.get('count', 0)} 条"
                    f"（任务累计 {got} 条）"))
            else:
                fails.append(f"来源{idx}：{cr.get('error', '采集失败')}")
                db.update_task(task_id, total=got, message=(
                    f"[{idx}/{len(urls)}] 采集失败，继续下一个…"))

        got = db.count_comments(task_id)
        msg = f"共 {len(urls)} 个来源，浏览器实得 {got} 条"
        if fails:
            msg += "；未完成：" + " | ".join(fails[:2])
        db.update_task(task_id, status="done", total=got,
                       finished_at=time.time(), message=msg)
        _CAPTURE_JOBS[job_id]["status"] = "done"
        _CAPTURE_JOBS[job_id]["result"] = {
            "ok": True, "keyword": keyword, "platform": platform,
            "urls": urls, "task_id": task_id, "task_ids": [task_id],
            "count": len(urls), "total": got, "fails": fails,
        }

    threading.Thread(target=_run, daemon=True).start()
    return job_id


def _auto_capture_loop(key: str, a: dict) -> None:
    """后台轮询浏览器，等用户登录后把该平台 Cookie 自动写回 settings.json。"""
    deadline = time.time() + 300  # 最多等 5 分钟
    while time.time() < deadline:
        if not browser_cdp.browser_alive():
            with _CAPTURE_LOCK:
                w = _CAPTURE_WATCHERS.get(key)
                if w:
                    w["done"] = True
                    w["error"] = "浏览器已关闭，未能读取到登录态（请保持浏览器打开直到显示「已接入」）"
            return
        r = browser_cdp.get_cookies(a["domains"], require_marker=a.get("login_marker", ""))
        if r.get("ok"):
            s = load_settings()
            s[a["setting"]] = r["cookie"]
            save_settings(s)
            with _CAPTURE_LOCK:
                w = _CAPTURE_WATCHERS.get(key)
                if w:
                    w["done"] = True
                    w["result"] = r
            return
        time.sleep(3)
    with _CAPTURE_LOCK:
        w = _CAPTURE_WATCHERS.get(key)
        if w:
            w["done"] = True
            w["error"] = "等待登录超时（5 分钟），可重新点「打开登录页」或手动粘贴 Cookie"


# ---------------- 采集任务 ----------------

def _run_collect(task_id: int, url: str, limit: int, settings: dict, sort: str = "hot",
                 incremental: bool = False) -> None:
    _run_collect_multi(task_id, [url], limit, settings, sort, incremental=incremental)


def _run_collect_multi(task_id: int, urls: list[str], limit: int, settings: dict,
                       sort: str, incremental: bool = False) -> None:
    """按来源列表依次采集，全部写入同一个任务。

    incremental=True 时用于「重新采集」：已存在的评论会被 insert_comments 自动跳过，
    只把新增评论入库，从而实现长期跟踪同一帖子的增量舆情。
    """
    try:
        base = db.count_comments(task_id) if incremental else 0
        platforms: list[str] = []
        titles: list[str] = []
        warnings: list[str] = []
        reasons: list[str] = []
        availables: list[int] = []
        total_added = 0

        for idx, url in enumerate(urls, 1):
            collector = route(url)
            if collector is None:
                warnings.append(f"来源 {idx} 无法识别平台，已跳过")
                continue
            platforms.append(collector.platform)
            label = f"[{idx}/{len(urls)}]" if len(urls) > 1 else ""
            db.update_task(task_id, message=f"{label} 正在抓取 {collector.label}…")

            def prog(got: int, msg: str, _i=idx, _n=len(urls), _c=collector) -> None:
                prefix = f"[{_i}/{_n}]" if _n > 1 else ""
                db.update_task(task_id, message=f"{prefix} {_c.label} {msg}（{got} 条）")

            try:
                res = collector.collect(url, limit=limit, progress=prog,
                                        settings=settings, sort=sort)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"来源 {idx} 失败：{exc}")
                continue
            rows = res.to_rows()
            total_added += db.insert_comments(task_id, rows)
            if res.source_title:
                titles.append(res.source_title[:50])
            if res.available:
                availables.append(res.available)
            warnings.extend(res.warnings or [])
            if not res.complete and res.stop_reason:
                reasons.append(f"来源{idx}：{res.stop_reason}")

        got = db.count_comments(task_id)
        if incremental:
            msg = f"重新采集完成：新增 {total_added} 条（累计 {got} 条）"
        elif len(urls) > 1:
            # 目标条数是「每个来源」的，不是总量。写成「实得 2400 / 目标 300」
            # 会被读成超抓了 8 倍，这里把口径点明。
            msg = f"共 {len(urls)} 个来源，每个来源目标 {limit} 条，实得 {got} 条"
        else:
            msg = f"实得 {got} / 目标 {limit} 条"
        if availables:
            msg += f"（平台显示评论总量 {sum(availables)} 条）"
        if reasons:
            msg += "；未达标：" + " | ".join(reasons[:2])
        if warnings:
            msg += "；" + "；".join(warnings[:3])

        db.update_task(
            task_id,
            source_title=" / ".join(dict.fromkeys(titles))[:120] or (urls[0][:60] if urls else ""),
            platform=platforms[0] if len(set(platforms)) == 1 else ("multi" if len(set(platforms)) > 1 else (platforms[0] if platforms else "")),
            status="done",
            total=got,
            finished_at=time.time(),
            message=msg,
            params=json.dumps({
                "limit": limit, "sort": sort, "requested": limit,
                "sources": urls, "source_count": len(urls),
                "added": total_added, "baseline": base,
                "available": sum(availables) if availables else 0,
                "stop_reasons": reasons, "warnings": warnings,
            }, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001
        db.update_task(task_id, status="error", finished_at=time.time(),
                       message=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


def start_collect(name: str, urls: list[str] | str, limit: int = 500,
                  sort: str = "hot", incremental: bool = False) -> int:
    settings = load_settings()
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in urls if u and u.strip()]
    if not urls:
        raise ValueError("没有可采集的链接")
    platform = ""
    c = route(urls[0])
    if c:
        platform = c.platform
    task_id = db.create_task(name or urls[0][:40], platform, urls[0],
                             {"limit": limit, "sort": sort, "sources": urls})
    t = threading.Thread(target=_run_collect_multi,
                         args=(task_id, urls, limit, settings, sort, incremental),
                         daemon=True)
    t.start()
    return task_id


def recollect(task_id: int, limit: int = 500, sort: str = "hot") -> tuple[bool, str]:
    """对已有任务重新采集（增量入库），用于长期跟踪同一个帖子。

    返回 (ok, error)。浏览器助手创建的任务（抖音/小红书/TapTap）不走服务端直连，
    直接给出提示，避免跑成「无法识别平台，已跳过」的空结果。
    """
    task = db.get_task(task_id)
    if not task:
        return False, "任务不存在"
    settings = load_settings()
    raw = task.get("params")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    raw = raw or {}
    if raw.get("browser"):
        return False, ("该任务由浏览器助手采集（抖音/小红书/TapTap），"
                       "请回到上方用「搜索并采集」重新发起")
    urls = raw.get("sources") or (
        [task["source_url"]] if task.get("source_url") else [])
    if not urls:
        return False, "该任务没有可复用的来源链接"
    db.update_task(task_id, status="running", message="正在重新采集…",
                   finished_at=None)
    t = threading.Thread(target=_run_collect_multi,
                         args=(task_id, urls, limit, settings, sort, True),
                         daemon=True)
    t.start()
    return True, ""


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CommunityPulse/1.0"
    # 是否已经开始发送响应头：用于避免在响应中途出错时再补一个 500（会拼成脏响应）
    _responded = False

    def log_message(self, fmt: str, *args) -> None:  # 静音默认日志
        pass

    # -- helpers --
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self._responded = True
        self.send_response(code)
        heads = [("Content-Type", ctype), ("Content-Length", str(len(body))),
                 ("Cache-Control", "no-store"),
                 # 允许浏览器采集助手从任意站点页面直接回传数据（仅监听 127.0.0.1）
                 ("Access-Control-Allow-Origin", "*"),
                 ("Access-Control-Allow-Headers", "Content-Type"),
                 ("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")]
        heads += list((extra or {}).items())
        for k, v in heads:
            v = str(v)
            # HTTP 头只能是 latin-1。带中文的值（例如下载文件的中文名）如果直接塞进去，
            # send_header 会抛 UnicodeEncodeError —— 而此时状态行和部分头已经写出去了，
            # 客户端会收到一个「200 开头的半截响应 + 500 响应」的拼接体（下载永远失败）。
            # 这里先校验、必要时转义，保证发头这一步永不抛异常。
            try:
                v.encode("latin-1")
            except UnicodeEncodeError:
                v = quote(v, safe="")
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _body(self) -> dict:
        """解析 JSON 请求体。非 dict 结构返回空字典以外的哨兵，由调用方拒绝。

        早期实现直接返回解析结果，遇到 JSON 数组 / 字符串 / null 时，
        `body.get(...)` 会抛 AttributeError，服务端退成 500 并把内部异常
        名抛给调用方。这里统一保证返回 dict，非 dict 一律视为空请求体
        （走参数缺失分支，返回 400 而不是 500）。
        """
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        if n > 32 * 1024 * 1024:   # 32MB 以上的请求体直接拒，避免无谓的内存占用
            return {}
        raw = self.rfile.read(n)
        try:
            parsed = json.loads(raw.decode("utf-8", "ignore"))
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _static(self, rel: str) -> bool:
        if rel in ("", "/"):
            rel = "index.html"
        target = (WEB_DIR / rel).resolve()
        # 必须按「路径分量」比较而不是字符串前缀：
        # "E:\app\web-backup\x".startswith("E:\app\web") 为真，
        # 会让 web 的兄弟目录绕过检查。is_relative_to 才是正确的包含判断。
        try:
            inside = target.is_relative_to(WEB_DIR.resolve())
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside = str(target).startswith(str(WEB_DIR.resolve()) + os.sep)
        if not inside:
            return False
        if not target.exists() or target.is_dir():
            return False
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if str(target).endswith((".html", ".css", ".js")):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)
        return True

    # -- routes --
    def do_GET(self) -> None:
        u = urlparse(self.path)
        # 必须解码：导出文件名带中文，浏览器会把 href 转成 %E5%8E%9F… 再发过来，
        # 不 unquote 就会拿 "原神…" 去拼路径而找不到文件（下载 404）。
        path, qs = unquote(u.path), parse_qs(u.query)
        try:
            # /exports/ 也要交给 _api_get：下载链接指向的是 data/exports 下的文件，
            # 而不是 web/ 目录里的静态资源，走静态分支会永远 404。
            if path.startswith("/api/") or path.startswith("/exports/"):
                self._api_get(path, qs)
            else:
                rel = path.lstrip("/")
                if not self._static(rel):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if self._responded:   # 响应头已发出：补发 500 只会拼出「200+500」脏响应
                return
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_POST(self) -> None:
        u = urlparse(self.path)
        try:
            self._api_post(unquote(u.path), self._body())
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            if self._responded:
                return
            self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        try:
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[1] == "tasks":
                db.delete_task(int(parts[2]))
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "不支持的操作"}, 400)
        except Exception as exc:  # noqa: BLE001
            if self._responded:
                return
            self._json({"ok": False, "error": str(exc)}, 500)

    def do_OPTIONS(self) -> None:
        self._send(204, b"", "text/plain")

    # -- API --
    def _api_get(self, path: str, qs: dict) -> None:
        if path == "/api/health":
            self._json({"ok": True, "jieba": HAS_JIEBA, "time": time.time()})
        elif path.startswith("/api/browser-capture/"):
            # 轮询浏览器采集任务进度：pending / running / done
            job_id = path.rsplit("/", 1)[1]
            job = _CAPTURE_JOBS.get(job_id)
            if not job:
                self._json({"ok": False, "error": "采集任务不存在或已过期"}, 404)
                return
            self._json({"ok": True, "status": job["status"],
                        "result": job["result"], "error": job["error"]})
        elif path.startswith("/api/browser-keyword-collect/"):
            # 轮询浏览器关键词采集（搜索 + 逐条抓评论）进度
            job_id = path.rsplit("/", 1)[1]
            job = _CAPTURE_JOBS.get(job_id)
            if not job:
                self._json({"ok": False, "error": "任务不存在或已过期"}, 404)
                return
            self._json({"ok": True, "status": job["status"],
                        "task_id": job.get("task_id"),
                        "result": job["result"], "error": job["error"]})
        elif path == "/api/platforms":
            self._json({"ok": True, "items": [
                {"platform": c.platform, "label": c.label, "reliability": c.reliability,
                 "hint": c.hint}
                for c in all_collectors() if c.platform != "generic"]})
        elif path == "/api/credentials":
            st = status_of(load_settings())
            with _CAPTURE_LOCK:
                for a in ACCESS:
                    w = _CAPTURE_WATCHERS.get(a["key"])
                    if w:
                        if w.get("running") and not w.get("done"):
                            st[a["key"]]["in_progress"] = True
                        if w.get("done") and w.get("error"):
                            st[a["key"]]["capture_error"] = w["error"]
            self._json({"ok": True,
                        "browser": bool(browser_cdp.browser_alive()),
                        "items": [dict(a, **st[a["key"]]) for a in ACCESS]})
        elif path == "/api/tasks":
            self._json({"ok": True, "items": db.list_tasks()})
        elif path.startswith("/api/tasks/"):
            tid = int(path.rsplit("/", 1)[1])
            t = db.get_task(tid)
            if not t:
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            t["count"] = db.count_comments(tid)
            self._json({"ok": True, "task": t})
        elif path.startswith("/api/analysis/"):
            tid = int(path.rsplit("/", 1)[1])
            a = db.get_latest_analysis(tid)
            self._json({"ok": True, "analysis": a})
        elif path.startswith("/api/export/"):
            tid = int(path.rsplit("/", 1)[1])
            fmt = (qs.get("fmt") or ["xlsx"])[0]
            t = db.get_task(tid)
            if not t:
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            a = db.get_latest_analysis(tid)
            if not a:
                self._json({"ok": False, "error": "请先执行一次分析"}, 400)
                return
            comments = db.get_comments(tid)
            if fmt == "xlsx":
                p = to_excel(t["name"], a["result"], comments)
            elif fmt == "csv":
                p = to_csv(t["name"], a["result"], comments)
            else:
                p = to_html_report(t["name"], a["result"], comments)
            # 文件名带中文，必须先在 href 里百分号编码：
            # 浏览器通常会帮忙转码，但 fetch / download / 复制到地址栏都不会，
            # 不编码就是 404。下载端已做 unquote，这里负责编码，两边对齐。
            self._json({"ok": True, "path": str(p), "name": p.name,
                        "url": f"/exports/{quote(p.name)}"})
        elif path.startswith("/exports/"):
            name = Path(path[len("/exports/"):]).name
            f = EXPORT_DIR / name
            if f.exists():
                ctype = "text/html; charset=utf-8" if f.suffix == ".html" else (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    if f.suffix == ".xlsx" else "text/csv; charset=utf-8")
                # 文件名带中文，必须按 RFC 5987 用 filename* 传（pure-ASCII），
                # 再给老浏览器留一个 ASCII 兜底名，避免响应头里出现非 latin-1 字符。
                ascii_name = name.encode("ascii", "ignore").decode() or f"report{f.suffix}"
                self._send(200, f.read_bytes(), ctype, {
                    "Content-Disposition":
                        f'attachment; filename="{ascii_name}"; '
                        f"filename*=UTF-8''{quote(name)}"})
            else:
                self._send(404, b"not found", "text/plain")
        elif path == "/api/settings":
            self._json({"ok": True, "settings": load_settings()})
        elif path == "/api/sentiment-memory":
            # 情感标签记忆：返回全部已记住的词条标签
            self._json({"ok": True, "memory": sentiment_memory.load_memory()})
        elif path == "/api/comment-memory":
            # 语句级情感记忆：列出已人工标注过的评论
            mem = comment_memory.load_memory()
            items = [{"key": k, "label": (v or {}).get("label"),
                      "text": (v or {}).get("text", ""), "at": (v or {}).get("at")}
                     for k, v in mem.items()]
            items.sort(key=lambda x: -(x.get("at") or 0))
            self._json({"ok": True, "count": len(items), "items": items})
        elif path.startswith("/api/comments/"):
            # 评论逐条（带记忆覆盖后的情感标签），供前端逐条查看 / 改标签
            tid = int(path.rsplit("/", 1)[1])
            raw_limit = (qs.get("limit") or ["300"])[0]
            try:
                limit = max(1, min(int(raw_limit), 2000))
            except (TypeError, ValueError):
                limit = 300
            rows = db.get_comments(tid, limit=limit)
            overrides = sentiment_memory.overrides_to_lexicon(sentiment_memory.load_memory())
            cmem = comment_memory.load_memory()
            items = []
            for r in rows:
                txt = r.get("content") or ""
                k = comment_memory.norm_key(txt)
                ml = comment_memory.label_of(cmem, k)
                label, score, _hits = score_text(txt, overrides)
                if ml:
                    label = ml
                items.append({
                    "id": r.get("id"), "content": txt[:300],
                    "user_name": r.get("user_name") or "",
                    "like_count": int(r.get("like_count") or 0),
                    "published_at": r.get("published_at"),
                    "key": k, "mem_label": ml, "sentiment": label,
                    "sent_score": score,
                })
            self._json({"ok": True, "total": db.count_comments(tid),
                        "returned": len(items), "items": items})
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)

    @staticmethod
    def _rank_videos(items: list[dict], platform: str) -> list[dict]:
        """按热度给搜索结果排序：取 PLATFORMS 里声明的 rank_key 字段降序；无指标则保持搜索顺序。"""
        cfg = DISCOVERY_PLATFORMS.get(platform, {})
        key = cfg.get("rank_key")
        if not key:
            return items
        return sorted(items, key=lambda v: (v.get(key) or 0), reverse=True)


    def _api_post(self, path: str, body: dict) -> None:
        if path == "/api/tasks":
            # 支持一次提交多个来源（批量采集），合并为一个任务
            raw_urls = body.get("urls")
            urls: list[str] = []
            if isinstance(raw_urls, list):
                urls = [str(u).strip() for u in raw_urls if str(u).strip()]
            if not urls:
                u = (body.get("url") or "").strip()
                if u:
                    urls = [u]
            if not urls:
                self._json({"ok": False, "error": "请填写至少一个链接"}, 400)
                return
            limit = int(body.get("limit") or 500)
            sort = (body.get("sort") or "hot").strip()
            if sort not in ("hot", "new", "both"):
                sort = "hot"
            tid = start_collect((body.get("name") or "").strip(), urls, limit, sort)
            self._json({"ok": True, "task_id": tid, "limit": limit, "sort": sort,
                        "source_count": len(urls)})
        elif path == "/api/browser-capture":
            # 抖音 / 小红书 等签名风控平台：拉起已登录浏览器自动采集并回传
            platform = (body.get("platform") or "").strip()
            url = (body.get("url") or "").strip()
            if platform not in ("douyin", "xiaohongshu"):
                self._json({"ok": False, "error": "该平台不支持浏览器采集（仅抖音/小红书）"}, 400)
                return
            if not url:
                self._json({"ok": False, "error": "请填写链接"}, 400)
                return
            if platform == "douyin" and "douyin.com" not in url and "iesdouyin.com" not in url:
                self._json({"ok": False, "error": "链接不是抖音视频页（需 https://www.douyin.com/video/xxxx）"}, 400)
                return
            if platform == "xiaohongshu" and "xiaohongshu.com" not in url and "xhslink.com" not in url:
                self._json({"ok": False, "error": "链接不是小红书笔记页（需 xiaohongshu.com/explore/... 或 xhslink.com）"}, 400)
                return
            target = max(1, min(int(body.get("target") or 500), 20000))
            job_id = start_browser_capture(platform, url, target)
            self._json({"ok": True, "job_id": job_id})
        elif path == "/api/discover":
            """关键词搜索：发现值得监测的来源（借鉴 MediaCrawler 的 search 模式）。"""
            kw = (body.get("keyword") or "").strip()
            if not kw:
                self._json({"ok": False, "error": "请填写关键词"}, 400)
                return
            platform = (body.get("platform") or "bilibili").strip()
            if platform not in DISCOVERY_PLATFORMS:
                self._json({"ok": False, "error": "该平台暂不支持关键词发现（支持："
                            + " / ".join(DISCOVERY_PLATFORMS.keys()) + "）"}, 400)
                return
            cfg = DISCOVERY_PLATFORMS.get(platform, {})
            settings = load_settings()
            if cfg.get("browser"):
                self._json({"ok": False, "error": f"{cfg.get('label')} 的搜索需在已登录浏览器中进行，请直接使用「搜索并采集」按钮"}, 200)
                return
            if cfg.get("search_unavailable"):
                self._json({"ok": False, "error": f"{cfg.get('label')} 暂不支持关键词搜索：{cfg.get('note', '')}"}, 200)
                return
            if cfg.get("needs_login") and not cookie_for(settings, platform):
                self._json({"ok": False, "error": f"{cfg.get('label')} 需要先登录：请在『平台接入』里点「打开登录页」登录后点「一键获取」"}, 200)
                return
            limit = max(1, min(int(body.get("limit") or 20), 50))
            sort = (body.get("sort") or "hot").strip()
            try:
                items = search(platform, kw, limit=limit, sort=sort, settings=settings)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 200)
                return
            self._json({"ok": True, "platform": platform, "keyword": kw,
                        "count": len(items), "items": items,
                        "reliability": cfg.get("reliability", ""),
                        "note": cfg.get("note", ""),
                        "empty_hint": ("该平台搜索接口当前不可达，可直接在上方粘贴链接采集"
                                       if not items else "")})
        elif path == "/api/keyword-collect":
            # 关键词 → 搜索 → 取热度前 N 个来源 → 逐条抓评论，合并为一个任务
            kw = (body.get("keyword") or "").strip()
            if not kw:
                self._json({"ok": False, "error": "请填写关键词"}, 400)
                return
            platform = (body.get("platform") or "bilibili").strip()
            if platform not in DISCOVERY_PLATFORMS:
                self._json({"ok": False, "error": "该平台暂不支持关键词采集（支持："
                            + " / ".join(DISCOVERY_PLATFORMS.keys()) + "）"}, 400)
                return
            cfg = DISCOVERY_PLATFORMS.get(platform, {})
            video_limit = max(1, min(int(body.get("video_limit") or 5), 30))
            comment_limit = max(10, min(int(body.get("comment_limit") or 300), 20000))
            sort = (body.get("sort") or "hot").strip()
            settings = load_settings()
            # 抖音 / 小红书：搜索也过风控，交给浏览器助手（搜索 + 逐条抓评论同一会话）
            if cfg.get("browser"):
                job_id = start_browser_keyword_collect(platform, kw, video_limit,
                                                      comment_limit, sort,
                                                      label=cfg.get("label", ""))
                self._json({"ok": True, "browser": True, "job_id": job_id,
                            "platform": platform, "keyword": kw,
                            "video_limit": video_limit, "comment_limit": comment_limit,
                            "message": f"已在已登录浏览器中搜索「{kw}」并抓取热度前 {video_limit} 条"
                                       f"（多来源合并为一个任务，每条最多 {comment_limit} 条评论）"})
                return
            if cfg.get("search_unavailable"):
                self._json({"ok": False, "error": f"{cfg.get('label')} 暂不支持关键词搜索：{cfg.get('note', '')}"}, 200)
                return
            # 需要登录的平台：没 Cookie 直接提示先登录（其余平台匿名也能搜）
            if cfg.get("needs_login") and not cookie_for(settings, platform):
                self._json({"ok": False, "error": f"{cfg.get('label')} 需要先登录：请在『平台接入』里点「打开登录页」登录后点「一键获取」"}, 200)
                return
            # 先多搜一些形成候选池，再按热度取前 N
            pool = search(platform, kw, limit=max(video_limit * 2, 20), sort=sort,
                          settings=settings)
            if not pool:
                self._json({"ok": False, "error": f"没有搜到「{kw}」相关结果（"
                            + cfg.get("note", "") + "）"}, 200)
                return
            ranked = self._rank_videos(pool, platform)[:video_limit]
            urls = [v["url"] for v in ranked]
            collect_sort = "new" if sort == "new" else "hot"
            name = f"{kw} · {cfg.get('label', '')}热度前{len(urls)}"
            try:
                tid = start_collect(name, urls, comment_limit, collect_sort)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 200)
                return
            self._json({"ok": True, "task_id": tid, "keyword": kw, "platform": platform,
                        "video_limit": len(urls), "comment_limit": comment_limit,
                        "videos": [dict(v) for v in ranked]})
        elif path.startswith("/api/tasks/") and path.endswith("/recollect"):
            try:
                tid = int(path.split("/")[3])
            except (IndexError, ValueError):
                self._json({"ok": False, "error": "任务 ID 无效"}, 400)
                return
            limit = int(body.get("limit") or 500)
            sort = (body.get("sort") or "hot").strip()
            if sort not in ("hot", "new", "both"):
                sort = "hot"
            ok, err = recollect(tid, limit, sort)
            self._json({"ok": ok, "error": err})
        elif path == "/api/preview":
            """同步试抓少量样本，方便先确认能不能抓到。"""
            url = (body.get("url") or "").strip()
            if not url:
                self._json({"ok": False, "error": "请填写链接"}, 400)
                return
            c = route(url)
            if c is None:
                self._json({"ok": False, "error": "无法识别平台"}, 400)
                return
            try:
                want = int(body.get("limit") or 20)
                sort = (body.get("sort") or "hot").strip()
                if sort not in ("hot", "new", "both"):
                    sort = "hot"
                res = c.collect(url, limit=want, settings=load_settings(), sort=sort)
                rows = res.to_rows()[:want]
                self._json({"ok": True, "platform": c.platform, "label": c.label,
                            "reliability": c.reliability, "title": res.source_title,
                            "warnings": res.warnings, "count": len(rows), "samples": rows,
                            "requested": want, "complete": res.complete,
                            "available": res.available,
                            "stop_reason": res.stop_reason, "capacity_hint": res.capacity_hint})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "platform": c.platform, "label": c.label,
                            "reliability": c.reliability, "error": f"{type(exc).__name__}: {exc}",
                            "hint": c.hint}, 200)
        elif path == "/api/import":
            raw = body.get("text") or ""
            fmt = body.get("fmt") or "auto"
            rows = parse_import(raw, fmt)
            if not rows:
                self._json({"ok": False, "error": "没有解析到任何评论，请检查格式"}, 400)
                return
            name = (body.get("name") or f"手动导入 {time.strftime('%m-%d %H:%M')}").strip()
            # 浏览器助手导出的 JSON 带平台/来源元信息，用它们还原真实的来源标记
            platform, src_url, src_title = "manual", "", ""
            if fmt in ("auto", "json") and raw.lstrip().startswith("{"):
                try:
                    meta = json.loads(raw)
                    if isinstance(meta, dict):
                        platform = str(meta.get("platform") or "manual")
                        src_url = str(meta.get("source_url") or "")
                        src_title = str(meta.get("source_title") or "")
                except Exception:  # noqa: BLE001
                    pass
            # 追加模式：浏览器关键词采集把多个视频/笔记的评论并进同一个任务，
            # 与服务端直连路径口径对齐（有效评论随来源数累加）。
            # 任务状态由发起方（start_browser_keyword_collect）统一维护，这里只入库。
            try:
                append_id = int(body.get("task_id") or 0)
            except (TypeError, ValueError):
                append_id = 0
            if append_id and db.get_task(append_id):
                n = db.insert_comments(append_id, rows)
                self._json({"ok": True, "task_id": append_id, "count": n,
                            "total": db.count_comments(append_id), "appended": True})
                return
            tid = db.create_task(name, platform, src_url, {"fmt": fmt})
            n = db.insert_comments(tid, rows)
            db.update_task(tid, status="done", total=n, finished_at=time.time(),
                           message=f"导入 {n} 条",
                           source_title=f"{src_title}（{name}）" if src_title else name)
            self._json({"ok": True, "task_id": tid, "count": n})
        elif path.startswith("/api/analyze/"):
            tid = int(path.rsplit("/", 1)[1])
            t = db.get_task(tid)
            if not t:
                self._json({"ok": False, "error": "任务不存在"}, 404)
                return
            comments = db.get_comments(tid)
            if not comments:
                self._json({"ok": False, "error": "该任务还没有评论数据"}, 400)
                return
            # 采集尚未结束时不要静默出结果：那样只会分析到当前已入库的一小部分
            # （多来源任务往往只抓完第 1~2 个），很容易被误当成全量。前端据 code=collecting 二次确认。
            if t.get("status") == "running" and not body.get("force"):
                # params.limit 是「每个来源」的上限，乘来源数才是本次总目标，否则会出现「已入库 600 / 目标 300」的矛盾提示
                target = 0
                try:
                    cfg = json.loads(t.get("params") or "{}") or {}
                    per = int(cfg.get("limit") or 0)
                    nsrc = len(cfg.get("sources") or []) or 1
                    target = per * nsrc
                except Exception:  # noqa: BLE001
                    target = 0
                self._json({
                    "ok": False, "code": "collecting",
                    "collected": len(comments), "target": target,
                    "error": f"该任务还在采集中，当前已入库 {len(comments)} 条；"
                             f"现在分析只会覆盖这部分数据",
                }, 200)
                return
            params = {k: v for k, v in body.items() if k not in ("task_id", "force")}
            result = analyze(comments, params)
            stats = result.setdefault("stats", {})
            # 本次分析条数 vs 任务库中总条数，便于一眼看出是否被截取
            stats["task_total"] = db.count_comments(tid)
            aid = db.save_analysis(tid, params, result)
            # 只更新提示语，且绝不覆盖 finished_at（那是采集完成时间，不属于分析）
            if t.get("status") == "done":
                db.update_task(tid, message=f"分析完成（本次分析 {stats.get('total', 0)} 条）")
            self._json({"ok": True, "analysis_id": aid, "result": result})
        elif path == "/api/settings":
            save_settings(body.get("settings") or {})
            self._json({"ok": True})
        elif path == "/api/sentiment-memory":
            """情感标签记忆：POST {word, label} 设置/更新（label=positive/neutral/negative）。GET 在 _api_get 中处理。"""
            word = (body.get("word") or "").strip()
            label = (body.get("label") or "").strip()
            if not word:
                self._json({"ok": False, "error": "请指定词条"}, 400)
                return
            if label not in ("positive", "neutral", "negative"):
                self._json({"ok": False, "error": "标签必须是 positive/neutral/negative"}, 400)
                return
            try:
                entry = sentiment_memory.set_label(word, label, by="user")
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            self._json({"ok": True, "word": word, "label": label, "entry": entry,
                        "message": "已记住该词条的情感标签，下次分析将自动应用"})
            return
        elif path == "/api/comment-sentiment":
            """语句级情感记忆：POST {key?, text?, label} 记住某条评论 / 语句的标签。

            key 由分析结果给出（基于完整正文算的指纹，避免前端截断导致对不上）；
            没有 key 时用 text 现算。
            """
            key = (body.get("key") or "").strip()
            text = (body.get("text") or "").strip()
            label = (body.get("label") or "").strip()
            if label not in ("positive", "neutral", "negative"):
                self._json({"ok": False, "error": "标签必须是 positive/neutral/negative"}, 400)
                return
            try:
                comment_memory.save(key=key, label=label, text=text, by="user")
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
                return
            self._json({"ok": True, "key": key or comment_memory.norm_key(text),
                        "label": label,
                        "message": "已记住这条评论的情感标签，重新分析或再次采集到相同内容时会自动应用"})
            return
        elif path == "/api/comment-memory/delete":
            """撤销一条语句记忆。"""
            key = (body.get("key") or "").strip()
            if not key:
                self._json({"ok": False, "error": "缺少 key"}, 400)
                return
            self._json({"ok": True, "removed": comment_memory.remove(key)})
            return
        elif path == "/api/credentials/open":
            """点登录：拉起带调试端口的浏览器并打开该平台的登录页。"""
            key = (body.get("platform") or "").strip()
            a = BY_KEY.get(key)
            if not a:
                self._json({"ok": False, "error": "未知平台"}, 400)
                return
            if a["kind"] == "key":
                webbrowser.open(a["login_url"])
                self._json({"ok": True, "manual": True,
                            "message": "已打开 API Key 申请页，拿到后粘贴到下方输入框"})
                return
            r = browser_cdp.launch(a["login_url"])
            if not r.get("ok"):
                self._json(r, 200)
                return
            # 启动后台自动捕获：登录后自动把 Cookie 写回 settings.json
            with _CAPTURE_LOCK:
                _CAPTURE_WATCHERS[key] = {"running": True, "done": False,
                                          "error": "", "result": None, "started": time.time()}
            threading.Thread(target=_auto_capture_loop, args=(key, a), daemon=True).start()
            self._json({"ok": True, "watching": True,
                        "message": "已打开登录页，登录后工具会自动读取登录态（请保持浏览器打开直到显示「已接入」）"})
        elif path == "/api/credentials/capture":
            """一键获取：从浏览器里读出该平台的 Cookie 并保存。"""
            key = (body.get("platform") or "").strip()
            a = BY_KEY.get(key)
            if not a or not a["setting"]:
                self._json({"ok": False, "error": "该平台不需要 Cookie"}, 400)
                return
            r = browser_cdp.get_cookies(a["domains"], require_marker=a.get("login_marker", ""))
            if not r.get("ok"):
                self._json(r, 200)
                return
            s = load_settings()
            s[a["setting"]] = r["cookie"]
            save_settings(s)
            self._json({"ok": True, "platform": key, "count": r["count"],
                        "fields": r["fields"],
                        "masked": status_of(s)[key]["masked"],
                        "message": f"已写入 {a['label']} 登录态（{r['count']} 个字段），下次采集生效"})
        elif path == "/api/credentials/save":
            """手动粘贴 Cookie / API Key。"""
            key = (body.get("platform") or "").strip()
            a = BY_KEY.get(key)
            if not a or not a["setting"]:
                self._json({"ok": False, "error": "该平台不需要凭据"}, 400)
                return
            s = load_settings()
            val = str(body.get("value") or "").strip()
            if val:
                s[a["setting"]] = val
            else:
                s.pop(a["setting"], None)
            save_settings(s)
            self._json({"ok": True, "state": status_of(s)[key]["state"]})
        elif path == "/api/credentials/clear":
            key = (body.get("platform") or "").strip()
            a = BY_KEY.get(key)
            if not a or not a["setting"]:
                self._json({"ok": False, "error": "该平台不需要凭据"}, 400)
                return
            s = load_settings()
            s.pop(a["setting"], None)
            save_settings(s)
            self._json({"ok": True})
        elif path == "/api/credentials/close":
            r = browser_cdp.quit_browser()
            self._json(r)
        elif path == "/api/merge":
            ids = body.get("task_ids") or []
            name = (body.get("name") or "合并任务").strip()
            if not ids:
                self._json({"ok": False, "error": "请选择要合并的任务"}, 400)
                return
            tid = db.create_task(name, "merged", "", {"from": ids})
            n = db.merge_task(tid, [int(i) for i in ids])
            db.update_task(tid, status="done", total=n, finished_at=time.time(),
                           message=f"合并 {n} 条", source_title=name)
            self._json({"ok": True, "task_id": tid, "count": n})
        else:
            self._json({"ok": False, "error": "未知接口"}, 404)


def _port_in_use(port: int) -> bool:
    """主动探测端口是否已有服务在监听。

    Windows 的 SO_REUSEADDR 允许多个进程绑定同一端口且不报错，会导致
    第二个实例静默抢不到请求（表现为"启动了但页面打不开"），因此必须
    在 bind 之前先连一次确认。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, port)) == 0


def _bind(port: int, tries: int = 20) -> tuple[ThreadingHTTPServer, int]:
    """绑定端口；被占用则自动顺延，避免用户双击时因端口冲突打不开。"""
    last: Exception | None = None
    for i in range(tries):
        p = port + i
        if _port_in_use(p):
            if i == 0:
                print(f"  端口 {port} 已被其他程序占用，自动尝试其他端口…")
            continue
        try:
            return ThreadingHTTPServer((HOST, p), Handler), p
        except OSError as exc:
            last = exc
    raise RuntimeError(f"端口 {port} ~ {port + tries - 1} 均无法绑定：{last}")


def serve(open_browser: bool = True, port: int = PORT) -> None:
    db.init_db()
    global APP_PORT
    srv, port = _bind(port)
    APP_PORT = port
    _reap_orphans(port)
    # 删除任务后 SQLite 不会把空间还给磁盘，长期累积会让库文件只增不减；
    # 启动时（此刻没有别的连接）顺手回收一次碎片。
    try:
        got = db.maybe_vacuum()
    except Exception:  # noqa: BLE001 - 回收失败不该阻止启动
        got = None
    if got:
        before, after = got
        print(f"  已回收数据库碎片：{before / 1024 / 1024:.1f} MB → {after / 1024 / 1024:.1f} MB")
    url = f"http://{HOST}:{port}/"
    print("=" * 56)
    print("  CommunityPulse 社区舆情雷达 已启动")
    print(f"  访问地址: {url}")
    print(f"  数据目录: E:\\CommunityPulse\\data")
    print("  关闭窗口即停止服务")
    print("=" * 56)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


if __name__ == "__main__":
    serve()
