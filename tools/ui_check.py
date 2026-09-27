"""前端 UI 冒烟检查：无头 Chrome 打开页面，检查 JS 是否报错、列表是否渲染，并截图。

用法：先启动服务，再运行本脚本（默认检查 8801）。
     venv\\Scripts\\python.exe tools\\ui_check.py [端口]
"""
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import browser_cdp  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8801
URL = f"http://127.0.0.1:{PORT}/"
SHOT = ROOT / "data" / "ui_check.png"
DBG = 9333

FAIL = []


def check(label, cond, info=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {info}" if info else ""))
    if not cond:
        FAIL.append(label)


exe = browser_cdp.find_browser()
if not exe:
    print("SKIP 没找到 Chrome / Edge")
    sys.exit(0)

# 无头模式启动，独立临时 profile，避免动到用户的登录浏览器
proc = subprocess.Popen(
    [exe, "--headless=new", f"--remote-debugging-port={DBG}",
     f"--user-data-dir={ROOT / 'data' / 'browser-profile-ui'}",
     "--no-first-run", "--no-default-browser-check", "--disable-gpu", URL],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

browser_cdp.DEBUG_PORT = DBG          # 让内置 CDP 客户端连到无头实例
try:
    ws_url = None
    for _ in range(40):
        time.sleep(0.5)
        try:
            targets = [t for t in browser_cdp._http_json("/json/list")
                       if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if targets:
                ws_url = targets[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            pass
    check("无头浏览器已就绪", bool(ws_url))
    if not ws_url:
        raise SystemExit(1)

    ws = browser_cdp._WS(ws_url, timeout=30)
    ws.connect()
    seq = [0]

    def send(method, params=None, wait=True):
        seq[0] += 1
        mid = seq[0]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not wait:
            return None
        for _ in range(200):
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg
        return None

    def js(expr):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return (((r or {}).get("result") or {}).get("result") or {}).get("value")

    send("Runtime.enable")
    send("Page.enable")
    # 页面可能已经加载完，等脚本就绪
    for _ in range(30):
        if js("document.readyState") == "complete" and js("typeof loadTasks"):
            break
        time.sleep(0.5)
    time.sleep(2.5)

    check("页面标题正确", "CommunityPulse" in (js("document.title") or ""), js("document.title"))
    check("app.js 已加载（函数可用）",
          js("typeof watchBrowserKeyword === 'function' && typeof loadTasks === 'function'"))
    check("关键词面板存在", js("!!document.querySelector('#kwPlatform')"))
    opts = js("document.querySelectorAll('#kwPlatform option').length") or 0
    check("平台下拉已填充（含浏览器平台）", opts >= 9, f"{opts} 个平台")
    check("支持浏览器采集的平台在列",
          js("Array.from(document.querySelectorAll('#kwPlatform option'))"
             ".map(o=>o.value).join(',').includes('taptap')"))
    tasks = js("document.querySelectorAll('.task').length") or 0
    check("任务列表已渲染", tasks > 0, f"{tasks} 条任务")

    # 直接调一次前端渲染函数，确认新逻辑不抛错
    js("document.querySelector('#kw').value='检查';"
       "document.querySelector('#discoverBox').innerHTML='<div class=mut>UI 检查</div>';")
    errs = js("(function(){try{loadTasks();return '';}catch(e){return String(e);}})()")
    check("重新渲染任务列表不报错", not errs, errs or "")

    # 截图存档，便于人工核对
    time.sleep(1.0)
    shot = send("Page.captureScreenshot", {"format": "png"})
    data = (((shot or {}).get("result") or {}).get("data"))
    if data:
        SHOT.write_bytes(base64.b64decode(data))
        check("已生成页面截图", SHOT.exists() and SHOT.stat().st_size > 5000,
              f"{SHOT.name} {SHOT.stat().st_size // 1024} KB")
    else:
        check("已生成页面截图", False, "未拿到截图数据")

    send("Browser.close", wait=False)
    ws.close()
finally:
    time.sleep(1)
    try:
        proc.terminate()
    except Exception:
        pass

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ -> {FAIL}"))
sys.exit(1 if FAIL else 0)
