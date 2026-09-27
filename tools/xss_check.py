"""前端 XSS 检查：把含攻击载荷的分析结果喂给真实浏览器渲染，逐个 tab 检查 DOM 是否被注入。

用法：先启动服务，构造含 XSS payload 的任务并分析，再运行
     venv\\Scripts\\python.exe tools/xss_check.py [任务id] [端口]
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import browser_cdp  # noqa: E402

TASK = int(sys.argv[1]) if len(sys.argv) > 1 else 45
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8801
URL = f"http://127.0.0.1:{PORT}/"
DBG = 9334
PROFILE = ROOT / "data" / "browser-profile-xss"

FAIL = []


def check(label, cond, info=""):
    print(("PASS " if cond else "FAIL ") + label + (f"  {info}" if info else ""))
    if not cond:
        FAIL.append(label)


exe = browser_cdp.find_browser()
if not exe:
    print("SKIP 没找到 Chrome / Edge")
    sys.exit(0)

proc = subprocess.Popen(
    [exe, "--headless=new", f"--remote-debugging-port={DBG}",
     f"--user-data-dir={PROFILE}",
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
        for _ in range(300):
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg
        return None

    def js(expr):
        r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                      "awaitPromise": True})
        return (((r or {}).get("result") or {}).get("result") or {}).get("value")

    send("Runtime.enable")
    send("Page.enable")
    for _ in range(40):
        if js("document.readyState") == "complete" and js("typeof render === 'function'"):
            break
        time.sleep(0.5)
    check("页面就绪且 render 可用", js("typeof render === 'function'"))

    # 拉取分析结果，交给前端自己的渲染函数
    loaded = js(
        "(async()=>{try{"
        f"const t=await (await fetch('/api/tasks/{TASK}')).json();"
        f"const a=await (await fetch('/api/analyze/{TASK}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:'{{}}'}})).json();"
        "if(!a.ok) return 'analyze失败: '+(a.error||'');"
        "state.result=a.result; state.taskId=%d; state.tab='kw'; render();"
        "return 'ok';}catch(e){return String(e)}})()" % TASK)
    check("分析结果已注入前端", loaded == "ok", str(loaded))

    tabs = js("(typeof TABS!=='undefined'?TABS.map(t=>t[0]):"
              "Array.from(document.querySelectorAll('.tabs button')).map(b=>b.dataset.tab))")
    check("已取到 tab 列表", bool(tabs), str(tabs))

    # 逐个 tab 渲染，检查 DOM 里是否出现真实的可执行元素
    # 只扫描渲染区内部：页面自身的 <script src=app.js> 等是合法标签，不算注入
    PROBE = """(function(){
      const bad = [];
      const roots = ['#result','#kwEx','#colRight','#taskList'].map(s=>document.querySelector(s))
                    .filter(Boolean);
      roots.forEach(root=>{
        root.querySelectorAll('*').forEach(el=>{
          const tag = el.tagName.toLowerCase();
          if (['img','svg','iframe','script','object','embed','video','audio'].includes(tag)) {
            bad.push('真标签:'+tag);
          }
          for (const a of el.attributes || []) {
            if (/^on/i.test(a.name)) bad.push('事件属性:'+a.name+'='+a.value.slice(0,40));
            if (/^(href|src)$/i.test(a.name) && /^\\s*javascript:/i.test(a.value||'')) {
              bad.push('伪协议:'+a.name+'='+a.value.slice(0,40));
            }
          }
        });
      });
      return bad.join(' | ') || 'clean';
    })()"""

    seen_inject = []
    for tab in (tabs or []):
        js(f"state.tab='{tab}'; renderTab();")
        time.sleep(0.4)
        probe = js(PROBE)
        check(f"tab [{tab}] 无 DOM 注入", probe == "clean", "" if probe == "clean" else probe)
        if probe != "clean":
            seen_inject.append((tab, probe))

    # payload 应该以纯文本形式展示（被 esc 转义）
    shown = js("document.body.innerText.includes('<img src=x onerror=')")
    check("payload 以纯文本呈现（说明已转义）", shown is True, str(shown))

    # 攻击载荷若真执行会弹出对话框，这里确认没有
    dialogs = js("window.__cpDialogs===undefined ? 'none' : window.__cpDialogs")
    check("未触发任何弹窗", dialogs == "none", str(dialogs))

    print()
    if seen_inject:
        print("发现注入点：")
        for tab, probe in seen_inject:
            print(f"  [{tab}] {probe}")

    send("Browser.close", wait=False)
    ws.close()
finally:
    try:
        proc.terminate()
    except Exception:
        pass

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ -> {FAIL}"))
sys.exit(1 if FAIL else 0)
