# -*- coding: utf-8 -*-
"""启动/停止 .bat 与生成器的一致性，以及 GBK + CRLF 编码规则。

make_bat.py 的文件头把话说得很重：「要改启动流程就改这里，不要直接编辑
根目录的 .bat —— 否则下次有人跑本脚本会把改动冲掉；反过来，直接改 .bat
而不改这里，两边也会慢慢分叉。」但「不分叉」此前只靠自觉 —— 没有任何
东西阻止有人顺手改了 启动.bat 而不改 make_bat.py，下次重跑生成器改动
静默丢失，启动行为跟着变。

Windows 批处理还有两个硬坑（文件头同样点名）：换行必须是 CRLF，裸 LF
会让命令边界错乱；含中文必须 GBK（cmd 默认代码页 936），UTF-8 会乱码
甚至破坏语法。这里把两条规则也钉住，防止有人把 write() 改成 UTF-8。

整个测试只读不写，零删除额度消耗。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_bat import STOP, START, ROOT, write  # noqa: E402

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


def main() -> int:
    print("== .bat 与生成器不分叉 ==")
    for name, template in (("启动.bat", START), ("停止.bat", STOP)):
        p = ROOT / name
        if not p.exists():
            ok(f"{name} 存在", False, "文件丢失，跑一次 tools/make_bat.py 可还原")
            continue
        actual = p.read_bytes()
        expected = template.replace("\n", "\r\n").encode("gbk")
        ok(f"{name} 与 make_bat 模板逐字节一致",
           actual == expected,
           "有分叉：有人改了 .bat 没改生成器（或反之）。"
           "以 make_bat.py 为准重跑一次生成器可还原。")

    print("\n== GBK + CRLF 编码规则（write 的两个硬约束）==")
    for name, template in (("启动.bat", START), ("停止.bat", STOP)):
        data = template.replace("\n", "\r\n").encode("gbk")
        # GBK 严格可解，中文不丢
        try:
            text = data.decode("gbk")
            ok(f"{name} GBK 严格解码通过", True)
        except UnicodeDecodeError as e:
            ok(f"{name} GBK 严格解码通过", False, str(e))
            continue
        # 反向：含中文的批处理按 UTF-8 严格解码应当失败 —— 这才证明确实是
        # GBK 而不是碰巧兼容的纯 ASCII。若哪天这条变绿了，说明模板里的
        # 中文全没了，title / 提示语会跟着丢。
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in template)
        if has_cjk:
            try:
                data.decode("utf-8")
                ok(f"{name} 非 UTF-8（含中文但 UTF-8 能解，编码规则可能被改）",
                   False, "GBK 字节恰好是合法 UTF-8？检查模板是否丢了中文")
            except UnicodeDecodeError:
                ok(f"{name} 非 UTF-8（中文只能按 GBK 读）", True)
        # 换行：所有 \n 都必须带前导 \r（CRLF），不允许裸 LF
        ok(f"{name} 无裸 LF（全部 CRLF）",
           b"\n" in data and all(
               data[i - 1:i] == b"\r" for i, b in enumerate(data) if b == 0x0A))

    print("\n== 批处理语法要点 ==")
    # bat 里 % 要写成 %%，否则被当变量展开。模板里 for 变量 %%v 是已知一例。
    ok("启动.bat 保留 %%v 转义（for 变量）", "%%v" in START)
    # 两条批处理各自引用的关键路径不能漂移
    ok("启动.bat 指向 app\\main.py", r"app\main.py" in START)
    ok("停止.bat 走 stop_instance（精确停止，不再无差别杀 87xx）",
       r"tools\stop_instance.py" in STOP)
    ok("启动.bat 优先用随包运行时", r"runtime\py\python.exe" in START)

    print("\n== write() 的落盘行为（写到临时副本，不动真实 .bat）==")
    # write 写死 ROOT/name，为不碰真实的 启动.bat，用废弃名字验证行为，
    # 写完即删（单个文件，不涉及批量删除额度）。
    scratch = "_make_bat_probe.bat"
    try:
        write(scratch, START)
        p = ROOT / scratch
        ok("write 产出与模板规则一致",
           p.read_bytes() == START.replace("\n", "\r\n").encode("gbk"))
        ok("write 落盘是 GBK 字节（非 UTF-8）",
           "社区舆情雷达".encode("gbk") in p.read_bytes())
    finally:
        try:
            (ROOT / scratch).unlink(missing_ok=True)
        except OSError:
            pass

    print(f"\n通过 {_ok} 项，失败 {_fail} 项")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
