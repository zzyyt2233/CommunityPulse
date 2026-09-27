"""浏览器凭据助手：用 Chrome/Edge 的 DevTools 协议自动取 Cookie。

纯标准库实现（自带最小 WebSocket 客户端），不装 Playwright / Selenium，
避免往 C 盘塞几百 MB 浏览器。

流程：启动带调试端口的浏览器 → 打开平台登录页 → 用户扫码登录 →
点「一键获取」→ CDP 读出该域名下的 Cookie → 写入 settings.json。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.parse
from pathlib import Path
from urllib.request import urlopen

DEBUG_PORT = 9222
PROFILE_DIR = Path(__file__).resolve().parents[2] / "data" / "browser-profile"


# ------------------------------- 最小 WebSocket 客户端 -------------------------------

class _WS:
    """RFC6455 客户端帧的最小实现：够跑 CDP 就行。"""

    def __init__(self, url: str, timeout: float = 15.0):
        self.url = url
        self.sock: socket.socket | None = None
        self.timeout = timeout
        self._buf = bytearray()

    def connect(self) -> None:
        assert self.url.startswith("ws://")
        rest = self.url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 9222)), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败：连接被关闭")
            head += chunk
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("WebSocket 握手失败（可能需要 --remote-allow-origins）")
        self._buf = bytearray(head.split(b"\r\n\r\n", 1)[1])

    def send(self, text: str) -> None:
        data = text.encode()
        n = len(data)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        if n < 126:
            head = bytes([0x81, 0x80 | n])
        elif n < 65536:
            head = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        assert self.sock
        self.sock.sendall(head + mask + masked)

    def _read(self, n: int) -> bytes:
        assert self.sock
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WebSocket 连接中断")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv(self) -> str:
        """读一条完整消息（自动处理分片 / ping）。"""
        parts = bytearray()
        while True:
            b0, b1 = self._read(2)
            opcode = b0 & 0x0F
            masked = b1 & 0x80
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            if masked:
                self._read(4)
            payload = self._read(ln)
            if opcode == 0x8:
                raise RuntimeError("WebSocket 被对端关闭")
            if opcode == 0x9:  # ping
                continue
            if opcode == 0xA:  # pong
                continue
            parts += payload
            if b0 & 0x80:  # FIN
                return parts.decode("utf-8", "ignore")

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass


# ------------------------------- CDP 封装 -------------------------------

def _http_json(path: str, timeout: float = 3.0):
    with urlopen(f"http://127.0.0.1:{DEBUG_PORT}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def browser_alive(timeout: float = 1.5) -> dict | None:
    """调试端口上是否已有浏览器在跑。"""
    try:
        return _http_json("/json/version", timeout)
    except Exception:
        return None


def find_browser() -> str:
    """在本机找 Chrome / Edge 可执行文件。"""
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if not base:
            continue
        cands += [
            Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        ]
    cands += [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for c in cands:
        if c and c.exists():
            return str(c)
    return ""


def launch(url: str, port: int = DEBUG_PORT) -> dict:
    """启动带调试端口的浏览器并打开登录页；已在跑则直接新开标签。"""
    if browser_alive():
        return {"ok": True, "reused": True, "message": "已复用正在运行的调试浏览器，并新开了登录页",
                "tab": open_tab(url)}
    exe = find_browser()
    if not exe:
        return {"ok": False, "error": "没找到 Chrome / Edge，请手动打开登录页并粘贴 Cookie"}
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={str(PROFILE_DIR)}",
           "--no-first-run", "--no-default-browser-check", "--disable-popup-blocking",
           "--remote-allow-origins=*", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"启动浏览器失败：{exc}"}
    for _ in range(40):
        time.sleep(0.5)
        if browser_alive():
            return {"ok": True, "reused": False,
                    "message": "已打开浏览器登录页，登录完成后回到这里点「一键获取」"}
    return {"ok": False, "error": "浏览器已启动但调试端口未就绪，请稍等几秒后重试「一键获取」"}


def open_tab(url: str) -> bool:
    """在已运行的调试浏览器里新开一个标签页。"""
    ver = browser_alive()
    if not ver or not ver.get("webSocketDebuggerUrl"):
        return False
    ws = _WS(ver["webSocketDebuggerUrl"])
    try:
        ws.connect()
        ws.send(json.dumps({"id": 1, "method": "Target.createTarget",
                            "params": {"url": url}}))
        ws.recv()
        return True
    except Exception:
        return False
    finally:
        ws.close()


def get_cookies(domains: list[str], require_marker: str = "") -> dict:
    """读出指定域名下的 Cookie，拼成请求头格式。

    require_marker 非空时，必须包含该「登录标记 Cookie」才视为真正登录成功
    （避免把游客 Cookie 误判成已登录）。
    """
    info = browser_alive()
    if not info:
        return {"ok": False, "error": "没有检测到调试浏览器，请先点「打开登录页」"}
    targets = []
    try:
        targets = [t for t in _http_json("/json/list") if t.get("type") == "page"]
    except Exception:
        pass
    if not targets:
        return {"ok": False, "error": "浏览器里没有可用标签页，请先点「打开登录页」"}

    all_cookies: list[dict] = []
    last_err = ""
    for t in targets[:6]:
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        ws = _WS(ws_url)
        try:
            ws.connect()
            ws.send(json.dumps({"id": 1, "method": "Network.enable"}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Network.getAllCookies"}))
            for _ in range(3):
                msg = json.loads(ws.recv())
                if msg.get("id") == 2:
                    all_cookies = (msg.get("result") or {}).get("cookies") or []
                    break
            if all_cookies:
                break
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        finally:
            ws.close()

    if not all_cookies:
        return {"ok": False, "error": f"浏览器没返回 Cookie（{last_err or '未知原因'}），可改用手动粘贴"}

    picked: dict[str, str] = {}
    for c in all_cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if not any(dom == d.lstrip(".") or dom.endswith("." + d.lstrip(".")) for d in domains):
            continue
        picked[c.get("name", "")] = c.get("value", "")
    if not picked:
        return {"ok": False,
                "error": f"浏览器里没有 {', '.join(domains)} 的 Cookie，请确认已在该网站登录"}

    # 登录标记校验：未真正登录（只有游客 Cookie）时不算成功，让后台继续轮询
    if require_marker and require_marker not in picked:
        return {"ok": False, "marker_missing": True,
                "error": f"还没登录：未检测到登录标记 {require_marker}，请先在该网站完成登录"}

    header = "; ".join(f"{k}={v}" for k, v in picked.items())
    return {"ok": True, "cookie": header, "count": len(picked),
            "fields": sorted(picked.keys())}


def quit_browser() -> dict:
    """关闭调试浏览器（用户点「关闭登录浏览器」时调用）。"""
    info = browser_alive()
    if not info or not info.get("webSocketDebuggerUrl"):
        return {"ok": True, "message": "没有运行中的调试浏览器"}
    try:
        ws = _WS(info["webSocketDebuggerUrl"])
        ws.connect()
        ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        try:
            ws.recv()
        except Exception:
            pass
        ws.close()
        return {"ok": True, "message": "已关闭登录浏览器"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"关闭失败：{exc}"}


# ------------------------------- 一键浏览器采集（抖音 / 小红书） -------------------------------

WEB_ROOT = Path(__file__).resolve().parents[2]
_SNIPPET = WEB_ROOT / "web" / "snippets" / "browser_capture.js"

# 平台 -> 用于匹配已打开标签页 host 的关键字
_HOST_TOKENS = {
    "douyin": ["douyin.com", "iesdouyin.com"],
    "xiaohongshu": ["xiaohongshu.com", "xhslink.com"],
    "taptap": ["taptap.cn", "taptap.com"],
}


def _capture_ws(token: str) -> str | None:
    """找到匹配平台 token 的内容页，返回其 WebSocket 调试地址。"""
    try:
        targets = [t for t in _http_json("/json/list")
                   if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        best = None
        for t in targets:
            u = (t.get("url") or "")
            if any(tok in u for tok in _HOST_TOKENS.get(token, [token])):
                best = t["webSocketDebuggerUrl"]
        return best
    except Exception:
        return None


def capture_via_browser(url: str, platform: str, target: int = 500,
                        base_url: str | None = None, timeout: int = 300,
                        script_text: str | None = None, task_id: int | None = None) -> dict:
    """一键浏览器采集：拉起已登录浏览器 → 打开链接 → 注入采集脚本 → 轮询至完成。

    task_id 非空时表示「追加模式」：抓到的评论并入该已有任务而不是新建任务。
    返回 {"ok": True, "count": int, "task_id": int, "total": int}
          或 {"ok": False, "error": str}
    """
    # 1) 启动 / 复用调试浏览器并打开链接
    launched = launch(url)
    if not launched.get("ok"):
        return {"ok": False, "error": launched.get("error", "启动浏览器失败")}

    # 2) 等页面就绪并找到内容页 target
    ws_url = None
    for _ in range(40):  # 最多等 20s
        ws_url = _capture_ws(platform)
        if ws_url:
            break
        time.sleep(0.5)
    if not ws_url:
        return {"ok": False, "error": f"没在浏览器里找到 {platform} 的内容页，请确认链接已正确打开"}

    # 3) 读取采集脚本
    if not script_text:
        try:
            script_text = _SNIPPET.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取采集脚本失败：{exc}"}

    # 4) 注入脚本（自动模式：不弹窗、用 base_url 直接回传）
    prefix = (f"window.__CP_AUTO=true;window.__CP_TARGET={int(target)};"
              f"window.__CP_BASE={json.dumps(base_url or '')};")
    if task_id:
        # 追加模式：本次抓到的评论并入已存在的任务（多来源合并口径）
        prefix += f"window.__CP_TASK_ID={int(task_id)};"
    expr = prefix + script_text
    try:
        ws = _WS(ws_url)
        ws.connect()
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": expr,
                                       "awaitPromise": False, "returnByValue": False}}))
        ws.recv()
        ws.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"注入采集脚本失败：{exc}"}

    # 5) 轮询 window.__CP_done（每次重新连页面，避免页面跳转导致旧连接失效）
    done = None
    waited = 0
    while waited < timeout:
        time.sleep(3)
        waited += 3
        cur = _capture_ws(platform)
        if not cur:
            continue
        try:
            ws = _WS(cur)
            ws.connect()
            ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                "params": {"expression": "window.__CP_done?window.__CP_done:null",
                                           "returnByValue": True}}))
            msg = json.loads(ws.recv())
            val = (((msg.get("result") or {}).get("result") or {}).get("value"))
            ws.close()
            if val:
                done = val
                break
        except Exception:
            try:
                ws.close()
            except Exception:
                pass

    if not done:
        return {"ok": False,
                "error": f"采集超时（{timeout}s 未结束）。浏览器已保持打开，请登录/检查后重新点采集"}
    try:
        res = json.loads(done) if isinstance(done, str) else done
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"采集结果解析失败：{done}"}
    if res.get("ok"):
        return {"ok": True, "count": int(res.get("count", 0) or 0),
                "task_id": res.get("task_id"),
                "total": int(res.get("total", 0) or 0)}
    return {"ok": False, "error": res.get("error", "采集未完成")}


# ------------------------------- 浏览器搜索（抖音 / 小红书） -------------------------------

# 平台 -> 搜索页 URL 构造器
_SEARCH_URLS = {
    "douyin": lambda kw: f"https://www.douyin.com/search/{urllib.parse.quote(kw)}/video",
    "xiaohongshu": lambda kw: f"https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(kw)}",
    "taptap": lambda kw: f"https://www.taptap.cn/search?kw={urllib.parse.quote(kw)}",
}

_SNIPPET_SEARCH = WEB_ROOT / "web" / "snippets" / "browser_search.js"


def search_via_browser(keyword: str, platform: str, top_n: int = 5,
                       base_url: str | None = None, timeout: int = 180) -> dict:
    """在已登录浏览器里按关键词搜索，收集前 top_n 条内容链接。

    返回 {"ok": True, "urls": [...]} 或 {"ok": False, "error": str}。
    """
    if platform not in _SEARCH_URLS:
        return {"ok": False, "error": f"平台 {platform} 不支持浏览器搜索"}
    launched = launch(_SEARCH_URLS[platform](keyword))
    if not launched.get("ok"):
        return {"ok": False, "error": launched.get("error", "启动浏览器失败")}

    # 等搜索页就绪并找到内容页 target
    ws_url = None
    for _ in range(40):
        ws_url = _capture_ws(platform)
        if ws_url:
            break
        time.sleep(0.5)
    if not ws_url:
        return {"ok": False, "error": f"没在浏览器里找到 {platform} 的搜索页，请确认搜索页已正确打开"}

    try:
        script_text = _SNIPPET_SEARCH.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"读取搜索脚本失败：{exc}"}

    prefix = (f"window.__CP_SEARCH_TARGET={int(top_n)};"
              f"window.__CP_PLATFORM={json.dumps(platform)};")
    expr = prefix + script_text
    try:
        ws = _WS(ws_url)
        ws.connect()
        ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        ws.recv()
        ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                            "params": {"expression": expr,
                                       "awaitPromise": False, "returnByValue": False}}))
        ws.recv()
        ws.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"注入搜索脚本失败：{exc}"}

    # 轮询 window.__CP_search_done
    done = None
    waited = 0
    while waited < timeout:
        time.sleep(3)
        waited += 3
        cur = _capture_ws(platform)
        if not cur:
            continue
        try:
            ws = _WS(cur)
            ws.connect()
            ws.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            ws.recv()
            ws.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                "params": {"expression": "window.__CP_search_done?window.__CP_search_done:null",
                                           "returnByValue": True}}))
            msg = json.loads(ws.recv())
            val = (((msg.get("result") or {}).get("result") or {}).get("value"))
            ws.close()
            if val:
                done = val
                break
        except Exception:
            try:
                ws.close()
            except Exception:
                pass

    if not done:
        return {"ok": False,
                "error": f"搜索超时（{timeout}s 未结束）。浏览器已保持打开，请登录/检查后重新点采集"}
    try:
        res = json.loads(done) if isinstance(done, str) else done
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": f"搜索结果解析失败：{done}"}
    if res.get("ok"):
        return {"ok": True, "urls": res.get("urls", [])}
    return {"ok": False, "error": res.get("error", "搜索未完成")}
