# -*- coding: utf-8 -*-
"""离线验证：启动日志落盘与轮转（app/core/logsetup.py）。

和 RS 那套是同一份逻辑，各自测一遍 —— 两个仓库的代码是独立维护的。
"""
import io
import pathlib
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.core import logsetup  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def ok(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {label}")
    else:
        FAIL += 1
        print(f"  ✗ 失败! {label}")
    return cond


class _FakeConsole:
    encoding = "utf-8"

    def __init__(self):
        self.buf = []

    def write(self, s):
        self.buf.append(s)

    def flush(self):
        pass


def main():
    d = ROOT / "temp" / "logsetup_test"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    lf = d / "app.log"

    lf.write_text("x" * 100, encoding="utf-8")
    logsetup.rotate(lf, max_bytes=1000, keep=3)
    ok("没写满时不动日志", lf.exists() and not (d / "app.1.log").exists())

    lf.write_text("y" * 2000, encoding="utf-8")
    logsetup.rotate(lf, max_bytes=1000, keep=3)
    ok("写满后归档为 app.1.log", (d / "app.1.log").exists() and not lf.exists())

    (d / "app.1.log").write_text("第一代", encoding="utf-8")
    lf.write_text("z" * 2000, encoding="utf-8")
    logsetup.rotate(lf, max_bytes=1000, keep=3)
    ok("旧 .1 顺移成 .2", (d / "app.2.log").read_text(encoding="utf-8") == "第一代")
    ok("新的顶到 .1", (d / "app.1.log").read_text(encoding="utf-8") == "z" * 2000)

    (d / "app.1.log").write_text("A", encoding="utf-8")
    (d / "app.2.log").write_text("B", encoding="utf-8")
    (d / "app.3.log").write_text("C", encoding="utf-8")
    lf.write_text("N" * 2000, encoding="utf-8")
    logsetup.rotate(lf, max_bytes=1000, keep=3)
    ok("超过保留份数不再往后堆",
       not (d / "app.4.log").exists() and (d / "app.3.log").read_text(encoding="utf-8") == "B")

    sink = logsetup._Sink(d / "tee.log")
    console = _FakeConsole()
    tee = logsetup._Tee(console, sink)
    tee.write("你好")
    sink.flush()
    ok("Tee 照常写控制台", "".join(console.buf) == "你好")
    ok("Tee 同时落盘", (d / "tee.log").read_text(encoding="utf-8") == "你好")
    ok("Tee 转发 encoding 属性", tee.encoding == "utf-8")
    ok("Tee 不冒充终端（isatty=False）", tee.isatty() is False)

    old_max = logsetup.MAX_BYTES
    logsetup.MAX_BYTES = 50
    try:
        auto = logsetup._Sink(d / "auto.log")
        at = logsetup._Tee(None, auto)
        at.write("a" * 40)
        ok("没到阈值不归档", not (d / "auto.1.log").exists())
        at.write("b" * 40)
        ok("写满自动归档", (d / "auto.1.log").exists()
           and (d / "auto.1.log").stat().st_size == 80)
        ok("归档后新文件从头开始", (d / "auto.log").exists()
           and (d / "auto.log").stat().st_size == 0)
    finally:
        logsetup.MAX_BYTES = old_max

    old_out, old_err = sys.stdout, sys.stderr
    try:
        target = logsetup.install(d / "inst.log")
        ok("install 返回实际日志路径", target == d / "inst.log")
        cur = sys.stdout
        again = logsetup.install(d / "inst2.log")
        ok("重复 install 不再生效", again is None and sys.stdout is cur)
        print("安装之后 print 应该也留一份")
        sys.stdout.flush()
        body = (d / "inst.log").read_text(encoding="utf-8")
        ok("install 后 print 落进日志", "安装之后 print 应该也留一份" in body)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        logsetup._installed = False

    shutil.rmtree(d, ignore_errors=True)
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
