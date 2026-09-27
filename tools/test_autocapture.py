"""模拟登录态自动捕获：浏览器登录后，后台线程应自动把 Cookie 写回 settings.json。"""
import sys, time, threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.core.browser_cdp as bcd
import app.core.config as cfg
from app.core import credentials
import app.server as srv
from pathlib import Path

# 清空 settings，确保从零开始
(cfg.DATA_DIR / "settings.json").write_text("{}", encoding="utf-8")

# 模拟浏览器：先未登录（只有游客 Cookie），1.2s 后完成登录
_state = {"alive": True, "logged_in": False}

def fake_alive():
    return {"webSocketDebuggerUrl": "ws://fake"} if _state["alive"] else None

def fake_get(domains, require_marker=""):
    if not _state["logged_in"]:
        return {"ok": False, "marker_missing": True, "error": "not logged in"}
    return {"ok": True, "cookie": f"{require_marker}=abc123; other=1",
            "count": 2, "fields": [require_marker, "other"]}

bcd.browser_alive = fake_alive
bcd.get_cookies = fake_get

a = credentials.BY_KEY["bilibili"]
t = threading.Thread(target=srv._auto_capture_loop, args=("bilibili", a), daemon=True)
t.start()

time.sleep(0.6)
print("登录前 settings:", cfg.load_settings())
assert "bilibili_cookie" not in cfg.load_settings(), "未登录不应写入"

_state["logged_in"] = True
time.sleep(2.5)

s = cfg.load_settings()
print("登录后 settings:", s)
assert s.get("bilibili_cookie") == "SESSDATA=abc123; other=1", "登录后应自动写入 Cookie"
print("\nPASS: 登录态已自动捕获并持久化到 settings.json")
