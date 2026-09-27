"""按 data/instance.json 记录的进程号精确停止正在运行的服务。

停止.bat 早期会无差别杀掉所有 87xx 端口的监听进程——那台机器上跑着别的服务时
就会被误伤。这里只读本项目自己写的标记文件，并且校验端口确实是本服务的，
确认无误后才发终止信号。

用法：python tools/stop_instance.py [端口]
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTANCE = ROOT / "data" / "instance.json"

# Windows 控制台命令（tasklist / netstat）的输出跟随系统代码页：简体中文是 GBK、
# 日文是 CP932、俄文是 CP866。一律用 errors="ignore" 解码，数字和英文不被破坏即可，
# 绝不能按 UTF-8 硬解——那会在中文系统上抛 UnicodeDecodeError，
# 异常被上层吞掉后会把「明明在跑的服务」误判成未运行。
_CMD_ENCODING = "gbk"


def _run(cmd: list[str], timeout: int = 15) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
        return out.decode(_CMD_ENCODING, errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def port_in_use(port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pid_alive(pid: int) -> bool:
    """判断进程是否存活（不发任何信号）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        # /NH 去掉表头；只要输出里出现这个 pid 就说明进程存在
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH"])
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_listening_on(port: int) -> int:
    """回退手段：instance.json 丢失/损坏时，按端口从 netstat 反查 pid。

    Windows 上 os.kill(pid, 0) 语义不可靠（某些版本会真的发信号），
    这里统一走 netstat -ano，只解析监听本机地址且端口完全匹配的行，
    避免 :8766 误配到 :87660。
    """
    if port <= 0:
        return 0
    if os.name == "nt":
        for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            addr = parts[1]
            if addr.rsplit(":", 1)[-1] == str(port):
                try:
                    return int(parts[-1])
                except ValueError:
                    return 0
        return 0
    # macOS / Linux：优先用 lsof，没有的话退到 ss
    out = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"])
    m = re.search(r"\d+", out)
    if m:
        return int(m.group(0))
    out = _run(["ss", "-ltnp", f"sport = :{port}"])
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else 0


def terminate(pid: int) -> bool:
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True, timeout=15,
            )
            return r.returncode == 0
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    want_port = int(sys.argv[1]) if len(sys.argv) > 1 and str(sys.argv[1]).isdigit() else 0

    pid = 0
    port = want_port
    if INSTANCE.exists():
        try:
            info = json.loads(INSTANCE.read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
            port = int(info.get("port") or 0) or want_port
        except Exception:  # noqa: BLE001
            print("[警告] instance.json 读不出来，改按端口查找。")
    else:
        print("[提示] 没有 data/instance.json，改按端口查找。")

    if want_port and port and want_port != port:
        print(f"记录的端口是 {port}，与指定的 {want_port} 不一致，已跳过。")
        return 1

    # 标记里记的 pid 可能已经死了（上次是强杀），这时不能就此放弃，
    # 而是回退到「按端口反查」，这样即使标记丢了一样能停下来。
    if not pid_alive(pid):
        if port:
            pid = pid_listening_on(port)
        if not pid:
            print("服务已不在运行。")
            INSTANCE.unlink(missing_ok=True)
            return 0
        print(f"[提示] instance.json 里的进程号已失效，按端口 {port} 重新定位到 PID {pid}。")

    # 二次确认目标确实在监听，避免 pid 被系统回收复用后误杀别的进程
    if port and not port_in_use(port):
        print(f"端口 {port} 已经没有监听，为避免误杀已跳过。")
        INSTANCE.unlink(missing_ok=True)
        return 0

    print(f"正在停止 CommunityPulse（PID {pid}，端口 {port}）...")
    if terminate(pid):
        for _ in range(20):
            if not pid_alive(pid) or not port_in_use(port):
                print("已停止。")
                INSTANCE.unlink(missing_ok=True)
                return 0
            time.sleep(0.3)
        print("进程可能仍在收尾，请稍候查看。")
        return 0
    print("终止失败，请到运行服务的窗口按 Ctrl+C。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
