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
ALERT_SHOT = ROOT / "data" / "ui_alert.png"
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

    # ---- 负面预警条 ----
    # 分析结果面板要真跑一次分析太慢，这里直接塞一份最小结果进 state 再 render()，
    # 检验的是「渲染这条路径不出错、三档配色和转义都对」，不是分析本身（那是
    # tools/test_negative_alert.py 的事）。
    def fake(level, headline, items):
        return json.dumps({"stats": {"total": 100, "positive": 40, "neutral": 30,
                                     "negative": 30, "positive_rate": 0.4,
                                     "negative_rate": 0.3},
                           "alerts": {"level": level, "headline": headline,
                                      "items": items, "warn_at": 0.25},
                           "clusters": [], "topics": [], "keywords": []},
                          ensure_ascii=False)

    js("state.tab='kw';")
    for lv, cn in (("high", "需处理"), ("medium", "留意"), ("low", "可控")):
        js(f"state.result = {fake(lv, '负面占比 30.0%', [{'kind': 'rate', 'label': '负面 30 条 / 共 100 条'}] )}; render();")
        cls = js("(document.querySelector('.alert-bar')||{}).className") or ""
        head = js("(document.querySelector('.alert-bar .alert-badge')||{}).textContent") or ""
        check(f"预警条 {lv} 档渲染出来且配色类名正确", f"alert-bar {lv}" in cls, cls)
        check(f"预警条 {lv} 档徽章文案是「{cn}」", head.strip() == cn, head.strip())

    ok = js("(function(){state.result=" + fake(
        "high", "负面 X", [{"kind": "cluster", "label": "<img src=x onerror=alert(1)>"}]
    ) + ";render();var b=document.querySelector('.alert-bar li');"
        "return b? b.querySelectorAll('img').length : -1;})()")
    check("预警条里的用户内容被转义（不能插进 img）", ok == 0, f"img 元素 {ok} 个")

    errs = js("(function(){try{state.result=null;render();return '';}catch(e){return String(e);}})()")
    check("清空结果后重新渲染不报错", not errs, errs or "")

    # 留一张带预警条的截图，方便人工核对配色。
    # 结果面板在右栏，800x600 的老视口根本照不到，这里临时放宽并滚过去。
    send("Emulation.setDeviceMetricsOverride",
         {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
    js("state.result=" + fake("high", "负面占比 42.3%，已越过 25% 的警戒线",
                              [{"kind": "rate", "label": "负面 42 条 / 共 100 条（42.3%）",
                                "detail": "正面 30 条、中性 28 条"},
                               {"kind": "cluster", "label": "「优化、闪退」一带 10 条里 8 条是负面",
                                "detail": "优化太差一直闪退，官方也不修"}])
       + ";render();")
    # 顶栏是吸顶的，直接对 #result 用 block:start 会把预警条顶到标题栏底下，
    # 所以自己算偏移：让预警条落在吸顶栏下面 90px 处。
    js("(function(){var el=document.querySelector('.alert-bar');"
       "var y=el.getBoundingClientRect().top+window.scrollY-90;window.scrollTo(0,y);})()")
    time.sleep(0.6)
    shot2 = send("Page.captureScreenshot", {"format": "png"})
    d2 = (((shot2 or {}).get("result") or {}).get("data"))
    if d2:
        ALERT_SHOT.write_bytes(base64.b64decode(d2))
        check("已生成预警条截图", ALERT_SHOT.exists() and ALERT_SHOT.stat().st_size > 5000,
              f"{ALERT_SHOT.name} {ALERT_SHOT.stat().st_size // 1024} KB")
    js("state.result=null;render();")
    send("Emulation.clearDeviceMetricsOverride")

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
