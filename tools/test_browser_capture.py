"""模拟 CDP 验证 capture_via_browser 的注入 + 轮询闭环（不真正启动浏览器）。"""
import sys
import time

sys.path.insert(0, ".")
import app.core.browser_cdp as b


# ---- 假的 WebSocket：模拟页面在第一次轮询返回 null，第二次返回已完成 ----
_poll = {"n": 0}


class FakeWS:
    def __init__(self, url, timeout=15.0):
        self.url = url
        self.last = ""

    def connect(self):
        pass

    def send(self, text):
        self.last = text

    def recv(self):
        if "window.__CP_done" in self.last:
            _poll["n"] += 1
            if _poll["n"] == 1:
                return '{"id":2,"result":{"result":{"value":null}}}'
            return '{"id":2,"result":{"result":{"value":"{\\"ok\\":true,\\"count\\":42,\\"task_id\\":7}"}}}'
        return '{"id":2,"result":{}}'

    def close(self):
        pass


# 替换底层依赖，避免真实启动浏览器
b._WS = FakeWS
b.launch = lambda url: {"ok": True, "reused": True}
b._capture_ws = lambda platform: "ws://fake"

_real_sleep = time.sleep
time.sleep = lambda s: None  # 跳过轮询间隔，加速测试

try:
    res = b.capture_via_browser(
        "https://www.douyin.com/video/abc123", "douyin",
        target=10, base_url="http://127.0.0.1:8766",
    )
finally:
    time.sleep = _real_sleep

print("capture_via_browser ->", res)
assert res == {"ok": True, "count": 42, "task_id": 7}, res
assert _poll["n"] >= 2, "应该至少轮询两次（首次 null，二次完成）"
print("OK: 注入+轮询闭环正确，poll 次数 =", _poll["n"])
