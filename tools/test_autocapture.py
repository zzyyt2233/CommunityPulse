"""模拟登录态自动捕获：浏览器登录后，后台线程应自动把 Cookie 写回 settings.json。

注意：整个测试把 config.DATA_DIR 指到 temp/ 下的临时目录，绝不碰真实的
data/settings.json —— 这里做的事就是「清空 settings 再从零写入」，指错地方
等于每次跑测试都把你的登录态抹掉一次。
"""
import pathlib
import shutil
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.core.browser_cdp as bcd          # noqa: E402
import app.core.config as cfg               # noqa: E402
from app.core import credentials            # noqa: E402
from app.core import secret                 # noqa: E402
import app.server as srv                    # noqa: E402

TMP = ROOT / "temp" / "autocapture"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)
_orig_dir = cfg.DATA_DIR
cfg.DATA_DIR = TMP

# 清空（临时目录里的）settings，确保从零开始
(TMP / "settings.json").write_text("{}", encoding="utf-8")

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

try:
    time.sleep(0.6)
    assert "bilibili_cookie" not in cfg.load_settings(), "未登录不应写入"

    _state["logged_in"] = True
    time.sleep(2.5)

    s = cfg.load_settings()
    assert s.get("bilibili_cookie") == "SESSDATA=abc123; other=1", \
        f"登录后应自动写入 Cookie，实际 {s.get('bilibili_cookie')!r}"

    # 真要落在 temp 里，顺便确认没跑到真实 data/ 去。
    target = TMP / "settings.json"
    assert target.exists(), "应该写进临时目录的 settings.json"
    on_disk = target.read_text(encoding="utf-8")
    # 加密只在 Windows 上做（DPAPI 是系统自带的），非 Windows 原样落盘是
    # app/core/secret.py 文件头写明的既定行为 —— 但「读回来必须完整」这条两边都不打折，
    # 本套件真正要验的是自动捕获这条链路，不是 DPAPI 本身（那由 test_secret_settings 管）。
    if secret._IS_WINDOWS:
        # 盘上是密文，所以不能直接找 abc123 —— 找得到反而说明加密没生效。
        assert "abc123" not in on_disk, \
            f"settings.json 里不该有明文 Cookie：{on_disk[:120]}"
        assert secret.PREFIX in on_disk, "凭据应该以密文形式落盘"
        how = "盘上为 DPAPI 密文"
    else:
        assert "abc123" in on_disk, "非 Windows 平台按既定行为明文落盘"
        assert secret.PREFIX not in on_disk, "非 Windows 平台不该出现密文前缀"
        how = "非 Windows 按既定行为明文落盘"
    assert cfg.load_settings().get("bilibili_cookie") == "SESSDATA=abc123; other=1", \
        "落盘的 Cookie 要能原样读回来"
    assert cfg.DATA_DIR == TMP, "DATA_DIR 必须一直指着临时目录"
    print(f"PASS: 登录态已自动捕获并持久化到 settings.json（{how}）")
finally:
    cfg.DATA_DIR = _orig_dir
    shutil.rmtree(TMP, ignore_errors=True)
