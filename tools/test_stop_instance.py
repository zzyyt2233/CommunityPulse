# -*- coding: utf-8 -*-
"""stop_instance 的解析与防御逻辑：端口精确匹配、畸形输入不炸、误杀防护。

停止服务是少数「会主动杀进程」的操作，做错方向就是误伤：把 :87660 配到
:8766、把 pid 被系统回收复用后的无辜进程当成自己的服务。stop_instance
为此写了三层防护（标记文件 + 端口反查 + 监听确认），其中解析与判断逻辑
是纯函数，可以不起任何真进程就验证 —— 这里用假输入喂它们。

Windows 控制台命令输出按系统代码页解码（GBK + errors=ignore）这个坑
（注释里写明：按 UTF-8 硬解会在中文系统上抛 UnicodeDecodeError，
把「明明在跑的服务」误判成未运行）也一并钉住。

整个测试不碰真进程、不碰真 instance.json（读路径只测「文件不存在」分支，
写路径根本不进 main()）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.stop_instance as si  # noqa: E402

_ok = 0
_fail = 0


def ok(label: str, cond: bool, info: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print("  ✓ " + label)
    else:
        _fail += 1
        print("  ✗ " + label + (f"  {info}" if info else ""))


# 仿真 netstat -ano 输出：覆盖完全匹配、相似端口、非监听、畸形行
FAKE_NETSTAT = "\r\n".join([
    "",
    "  活动连接",
    "  协议  本地地址          外部地址        状态           PID",
    "  TCP    127.0.0.1:8766     0.0.0.0:0      LISTENING       1234",
    "  TCP    127.0.0.1:87660    0.0.0.0:0      LISTENING       5678",
    "  TCP    192.168.1.5:8766   1.2.3.4:5555   ESTABLISHED     9999",
    "  TCP    [::1]:8766         [::]:0         LISTENING       8888",
    "  TCP    bad-line-with-few-cols",
    "",
])


def main() -> int:
    orig_run = si._run

    print("== 入参防御 ==")
    ok("port_in_use(0) 直接 False（不发起连接）", si.port_in_use(0) is False)
    ok("port_in_use(-1) 直接 False", si.port_in_use(-1) is False)
    ok("pid_alive(0) 直接 False", si.pid_alive(0) is False)
    ok("pid_alive(-5) 直接 False", si.pid_alive(-5) is False)
    ok("pid_listening_on(0) 直接 0", si.pid_listening_on(0) == 0)
    ok("pid_listening_on(-1) 直接 0", si.pid_listening_on(-1) == 0)

    if os.name == "nt":
        print("\n== netstat 解析（Windows）==")
        si._run = lambda cmd, timeout=15: FAKE_NETSTAT
        try:
            got = si.pid_listening_on(8766)
            ok(":8766 精确匹配到 PID 1234", got == 1234, f"解析出 {got}")
            ok(":87660 没有被误配成 :8766",
               si.pid_listening_on(87660) == 5678)
            got = si.pid_listening_on(8766)
            ok("ESTABLISHED 行不算（只认 LISTENING）", got == 1234)
            ok("找不到目标端口返回 0", si.pid_listening_on(7777) == 0)
            ok("畸形行被跳过而不是抛异常",
               si.pid_listening_on(8766) == 1234)
        finally:
            si._run = orig_run
        # 专门的防误配场景：库里只有 :87660 在监听，查 :8766 必须是 0。
        # 上面 FAKE_NETSTAT 里 :8766 排在前面，就算匹配逻辑错成「包含」，
        # 也因为先命中正确行而测不出来 —— 这条让模糊匹配无处遁形。
        si._run = lambda cmd, timeout=15: (
            "  TCP    127.0.0.1:87660   0.0.0.0:0   LISTENING   5678\r\n")
        try:
            ok("只有 :87660 监听时查 :8766 返回 0（相似端口不误配）",
               si.pid_listening_on(8766) == 0,
               f"误配成 {si.pid_listening_on(8766)}")
        finally:
            si._run = orig_run

        print("\n== tasklist 判活（Windows）==")
        si._run = lambda cmd, timeout=15: f"python.exe    1234 Console    1    25,000 K\r\n"
        try:
            ok("输出含 pid → 判活 True", si.pid_alive(1234) is True)
        finally:
            si._run = orig_run
        si._run = lambda cmd, timeout=15: "信息: 没有运行的任务匹配指定标准。"
        try:
            ok("输出不含 pid → 判活 False", si.pid_alive(1234) is False)
        finally:
            si._run = orig_run

    # 不整组跳过：netstat/tasklist 是 Windows 分支，但 CI 有 ubuntu 腿，
    # 那边走的是 lsof / ss 分支 —— 同样用假输入喂，跳过一个平台就等于
    # 那个平台的解析门禁是空的。os 是模块引用，替换 si.os 只影响本模块
    # 的查找，finally 恢复。
    print("\n== lsof / ss 解析（POSIX 分支，任意平台都能喂）==")
    import types
    real_os = si.os
    si.os = types.SimpleNamespace(name="posix")
    try:
        si._run = lambda cmd, timeout=15: "1234\n"
        try:
            ok("lsof 有输出 → 返回 pid", si.pid_listening_on(8766) == 1234)
        finally:
            si._run = orig_run
        si._run = lambda cmd, timeout=15: ""
        try:
            ok("lsof 空输出回退到 ss（ss 也空 → 0）",
               si.pid_listening_on(8766) == 0)
        finally:
            si._run = orig_run
        si._run = lambda cmd, timeout=15: "users:((\"python\",pid=4321,fd=3))"
        try:
            ok("ss 的 pid= 提取正确", si.pid_listening_on(8766) == 4321)
        finally:
            si._run = orig_run
    finally:
        si.os = real_os

    print("\n== 命令输出解码（系统代码页坑）==")
    # 模拟中文 Windows 的 tasklist 输出是 GBK 字节：按 UTF-8 硬解会炸，
    # _run 必须容忍。_run 接的是 subprocess，这里绕开它直接验解码语义：
    # GBK 字节 + errors=ignore 必须不抛异常，且其中的 ASCII（pid 数字）不损。
    gbk_out = "python.exe  1234  没有运行的任务匹配".encode("gbk")
    try:
        text = gbk_out.decode(si._CMD_ENCODING, errors="ignore")
        ok("GBK 输出按系统代码页解码不抛异常", True)
        ok("解码后 pid 数字完整", "1234" in text)
        try:
            gbk_out.decode("utf-8")
            ok("GBK 字节不是合法 UTF-8（证明硬解会炸的前提成立）", False,
               "这批字节恰好是 UTF-8？构造的样本有问题")
        except UnicodeDecodeError:
            ok("GBK 字节不是合法 UTF-8（证明硬解会炸的前提成立）", True)
    except UnicodeDecodeError as e:
        ok("GBK 输出按系统代码页解码不抛异常", False, str(e))

    print("\n== terminate 的失败容错 ==")
    # 不真杀进程：让 taskkill 一定失败（指向不存在的 pid 会得到非零码），
    # 验证 terminate 返回 False 而不是抛异常。
    ok("terminate(不存在的 pid) 返回 False 而非抛异常",
       si.terminate(99999999) is False)

    print(f"\n通过 {_ok} 项，失败 {_fail} 项")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
