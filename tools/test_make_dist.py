# -*- coding: utf-8 -*-
"""分发包排除规则：确认该进包的在、不该进包的一个都不在。

make_dist.py 的 SKIP_DIRS / SKIP_SUFFIX / SKIP_FILES 是一条明确的安全承诺 ——
data/ 里有采集数据和**登录过的浏览器 profile**（含平台 Cookie），一次打包发出去
就是账号泄漏。这条承诺此前只靠人肉读代码确认，没有任何测试守着。

collect_files() 是纯路径判断，可测性很好。这里造一棵「什么都有一点」的假目录树：
正向验证业务文件确实进包，反向验证每一类敏感文件都被挡住，另外把
include_data 开关（--with-data 备份模式）的两个方向都验一遍 ——
默认必须排除，显式打开才包含，这个语义搞反了同样是泄漏。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_dist import collect_files  # noqa: E402

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


def _touch(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def build_tree(root: Path) -> None:
    # --- 该进包的 ---
    _touch(root / "app.py")
    _touch(root / "requirements.txt")
    _touch(root / "README.md")
    _touch(root / "app" / "server.py")
    _touch(root / "app" / "core" / "db.py")

    # --- 目录级：一律不进 ---
    _touch(root / "venv" / "pyvenv.cfg")
    _touch(root / ".venv" / "pyvenv.cfg")
    _touch(root / "logs" / "run.log")
    _touch(root / "__pycache__" / "x.pyc")
    _touch(root / "app" / "__pycache__" / "y.pyc")
    _touch(root / ".git" / "config")
    _touch(root / "dist" / "pkg.zip")
    _touch(root / "build" / "x.tmp")
    _touch(root / ".idea" / "workspace.xml")
    _touch(root / ".vscode" / "settings.json")
    _touch(root / ".pytest_cache" / "v" / "cache.json")

    # --- 后缀级：不进 ---
    _touch(root / "app.pyc")
    _touch(root / "helper.pyo")
    _touch(root / "debug.log")
    _touch(root / "cache.db")
    _touch(root / "metrics.sqlite")
    _touch(root / "legacy.sqlite3")

    # --- 文件级：不进 ---
    _touch(root / ".deps_installed")
    _touch(root / "instance.json")
    _touch(root / "ui_check.png", "png")
    _touch(root / ".DS_Store")
    _touch(root / "app" / "instance.json", "nested")   # 嵌套位置也挡

    # --- data/：凭据与真实数据的所在地 ---
    _touch(root / "data" / "pulse.db")
    _touch(root / "data" / "instance.json")
    _touch(root / "data" / "browser_profile" / "Default" / "Cookies", "cookie")
    _touch(root / "data" / "browser_profile" / "Default" / "Login Data", "pw")
    _touch(root / "data" / "settings.json", '{"bilibili_cookie":"SESSDATA=real"}')


def main() -> int:
    print("== 分发包排除规则 ==")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        build_tree(root)

        # 默认模式：不打包 data/
        files = collect_files(root, include_data=False)
        names = {z for _, z in files}

        print("\n-- 该进包的必须都在 --")
        for keep in ("app.py", "requirements.txt", "README.md",
                     "app/server.py", "app/core/db.py"):
            ok(f"保留 {keep}", keep in names, f"实际收集 {len(names)} 个")

        print("\n-- 目录级排除 --")
        for d in ("venv", ".venv", "logs", "__pycache__", ".git", "dist",
                  "build", ".idea", ".vscode", ".pytest_cache"):
            hit = [n for n in names if d in Path(n).parts]
            ok(f"不含 {d}/ 里的任何文件", not hit, f"漏了 {hit[:2]}")

        print("\n-- 后缀级排除 --")
        for suf in (".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"):
            hit = [n for n in names if n.lower().endswith(suf)]
            ok(f"不含 *{suf}", not hit, f"漏了 {hit[:2]}")

        print("\n-- 文件级排除 --")
        for f in (".deps_installed", "instance.json", "ui_check.png",
                  ".DS_Store"):
            hit = [n for n in names if Path(n).name == f]
            ok(f"不含 {f}（含嵌套位置）", not hit, f"漏了 {hit[:2]}")

        print("\n-- data/ 是安全承诺的核心：默认绝不能进包 --")
        hit = [n for n in names if "data" in Path(n).parts]
        ok("默认模式完全不含 data/ 里的任何文件", not hit, f"漏了 {hit[:3]}")
        ok("找不到登录 Cookie（data/browser_profile/.../Cookies）",
           not any("Cookies" in n or "Login Data" in n for n in names))
        ok("找不到采集数据库（pulse.db）",
           not any(n.endswith("pulse.db") for n in names))

        # 显式开关：--with-data 时才包含（备份迁移用）
        files_d = collect_files(root, include_data=True)
        names_d = {z for _, z in files_d}
        print("\n-- include_data=True（备份模式）时才包含 data/ --")
        ok("打开开关后 data/pulse.db 进包",
           "data/pulse.db" in names_d,
           f"实际 {sorted(n for n in names_d if n.startswith('data/'))[:3]}")

        print("\n-- 收集结果结构正确 --")
        ok("返回的是 (磁盘路径, zip 相对路径) 二元组",
           all(isinstance(p, Path) and isinstance(z, str)
               for p, z in files))
        ok("zip 内路径全用正斜杠（跨平台解压一致）",
           not any("\\" in z for _, z in files))
        ok("磁盘路径都是真实存在的文件",
           all(os.path.isfile(p) for p, _ in files))

    print(f"\n通过 {_ok} 项，失败 {_fail} 项")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
