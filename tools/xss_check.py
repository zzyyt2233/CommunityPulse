"""前端 XSS 检查：把含攻击载荷的分析结果喂给真实浏览器渲染，逐个 tab 检查 DOM 是否被注入。

用法：先启动服务，再运行
     venv\\Scripts\\python.exe tools/xss_check.py [任务id] [端口]

不给任务 id 时脚本会自己造一个带载荷的任务，跑完再删掉。
以前是硬编码任务 45，那个任务一被删脚本就报三项假失败 ——
自检脚本必须自带测试数据，不能依赖库里碰巧有什么。
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core import browser_cdp  # noqa: E402

PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8801
URL = f"http://127.0.0.1:{PORT}/"
BASE = f"http://127.0.0.1:{PORT}"
DBG = 9334
PROFILE = ROOT / "data" / "browser-profile-xss"

FAIL = []

PAYLOAD = '<img src=x onerror="alert(1)">'


def api(method, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": str(e)}


def make_probe_task():
    """造一个带 XSS 载荷的任务并返回它的 id。

    载荷放在正文里，分析后会出现在词条、聚类代表句、重点原句这些位置，
    前端每个 tab 都渲染一遍就能验到。
    """
    lines = [
        f"这次更新{PAYLOAD}卡得根本没法玩",
        f"闪退问题{PAYLOAD}一直没修",
        f"优化太差了{PAYLOAD}",
        f"手机发烫{PAYLOAD}希望重视",
        "剧情和美术都挺好，就是性能不行",
        "抽卡概率感觉比上个版本低了",
        "服务器又炸了，排了十分钟队",
        "新手引导太长了，建议可以跳过",
    ]
    st, j = api("POST", "/api/import",
                {"text": "\n".join(lines), "name": "XSS 自检（可删）", "fmt": "text"})
    if st != 200 or not j.get("ok"):
        print(f"造测试任务失败：HTTP {st} {j}")
        sys.exit(1)
    tid = j.get("task_id") or j.get("id")
    if tid:
        return int(tid)
    # 返回体里没带 id 时，取列表里最新的那个
    st2, j2 = api("GET", "/api/tasks")
    items = j2.get("items") or []
    if not items:
        print("造完任务却查不到任务列表")
        sys.exit(1)
    return int(max(items, key=lambda t: t.get("id") or 0)["id"])


TASK = int(sys.argv[1]) if len(sys.argv) > 1 else make_probe_task()
print(f"使用任务 #{TASK}（未指定时由脚本自建，跑完会删掉）\n")


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

    # 导入层会先把 HTML 标签剥掉（实测带载荷的正文进库后只剩 10 个字符），
    # 所以载荷根本到不了分析结果里 —— 原来那条「payload 以纯文本呈现（说明已转义）」
    # 的断言永远不可能成立，是假失败。改成验证剥离本身生效：这才是真正挡住第一道的地方。
    _, aj = api("POST", f"/api/analyze/{TASK}", {})
    raw_ana = json.dumps(aj, ensure_ascii=False)
    check("导入层已剥离 HTML 标签（载荷进不了分析结果）",
          "<img" not in raw_ana and "<script" not in raw_ana,
          "" if "<img" not in raw_ana else "结果里仍有 <img")

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

# 自建的任务用完就删，别在任务列表里留垃圾
if len(sys.argv) <= 1:
    st, _ = api("DELETE", f"/api/tasks/{TASK}")
    print(f"\n已清理自检任务 #{TASK}（HTTP {st}）")

print("\n" + ("全部通过 ✅" if not FAIL else f"失败 {len(FAIL)} 项 ❌ -> {FAIL}"))
sys.exit(1 if FAIL else 0)
