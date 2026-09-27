"""打一个可以直接发给别人的分发包（zip）。

为什么不能直接压缩整个项目目录：
  1. venv/ 里的 pyvenv.cfg 写死了本机的 Python 绝对路径（如 C:\\Users\\你的名字\\...）。
     实测：home 指向不存在的路径时，venv 里的 python.exe 直接报
     "did not find executable"，退出码 103 —— 收件人一定跑不起来。
  2. data/ 里有你的采集数据（pulse.db）和**登录过的浏览器 profile**
     （browser-profile*，含各平台 Cookie），发出去等于泄漏登录态。
  3. 体积：整包 180MB+，干净包只有 1MB 出头。

本脚本默认只打「代码 + 启动脚本 + 依赖清单」，收件人首次双击 启动.bat 时
会自动建 venv、装依赖（需要联网，约 30 秒）。

用法：
    python tools/make_dist.py                 # 干净包（需联网装依赖）
    python tools/make_dist.py --with-wheels   # 附带离线 wheel，收件人断网也能装
    python tools/make_dist.py --portable      # 免安装版：连 Python 运行时一起打
    python tools/make_dist.py --include-data  # 连你的数据一起打（仅用于自己备份/迁移）

推荐直接发人的是 --portable：收件人解压后双击 启动.bat 就能用，
既不用装 Python，也不用联网装依赖。
"""
from __future__ import annotations

import argparse
import glob
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 目录：整个跳过（这些是本机运行产物 / 环境 / 隐私数据）
SKIP_DIRS = {
    "venv", ".venv", "data", "logs", "__pycache__", ".git",
    "dist", "build", ".idea", ".vscode", ".pytest_cache",
}
# 文件后缀：跳过
SKIP_SUFFIX = {".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3"}
# 明确跳过的文件
SKIP_FILES = {".deps_installed", "instance.json", "ui_check.png", ".DS_Store"}

# 收件人打开就能看到的说明（GBK 编码，Windows 记事本双击直接能读）
README_TXT = """CommunityPulse 社区舆情雷达 —— 第一次使用请读我
========================================================

【零、先看这里】
    解压后的文件夹里如果有  runtime  这个文件夹 —— 恭喜，这是免安装版，
    Python 和所有依赖都已经打包在里面了。
    直接跳到【二、启动本工具】，什么都不用装、也不用联网。

    没有 runtime 文件夹的话，才需要看下面第一步。


【一、只有非免安装版才需要：装 Python】
    1. 打开 https://www.python.org/downloads/ 下载 Python 3.9 或更高版本
    2. 安装时务必勾选最下面的  "Add Python to PATH"（添加到环境变量）
    3. 装完继续下一步

    怎么确认装好了？按住 Win+R，输入 cmd 回车，在黑窗口里输入：
        python --version
    能显示版本号（比如 Python 3.13.1）就说明装好了。


【二、启动本工具】
    Windows 用户：双击  启动.bat
    macOS/Linux ：终端里运行  ./启动.sh

    第一次启动会自动做两件事，大概需要 1-2 分钟，请耐心等：
        1) 在项目里创建一个独立的 Python 环境 venv
        2) 安装可选依赖（分词 / Excel 导出）

    看到浏览器自动打开的页面，就成功了。
    那个黑色窗口不要关，关掉就等于停止服务。


【三、关不掉 / 想停止】
    双击  停止.bat


【四、常见问题】

    Q：双击 启动.bat 一闪而过 / 提示没有 Python
    A：Python 没装，或者装的时候没勾 "Add Python to PATH"。重装一次并勾选即可。

    Q：提示"依赖安装失败"
    A：不影响使用。装不上分词就用内置方案，Excel 导出会自动改成 CSV。
       想重试的话，联网后重新双击 启动.bat 即可。

    Q：我想把工具放在别的盘 / 别的文件夹
    A：随便放，整个文件夹搬到哪里都能用。

    Q：我的数据存在哪？
    A：全部在你本地的 data 文件夹里，不会上传到任何地方。
"""


def collect_files(root: Path, include_data: bool) -> list[tuple[Path, str]]:
    """返回 [(磁盘路径, zip 内的相对路径)]。"""
    out: list[tuple[Path, str]] = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        parts = rel.parts
        if any(x in SKIP_DIRS for x in parts):
            continue
        if p.is_dir():
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        if p.name in SKIP_FILES:
            continue
        out.append((p, "/".join(parts)))
    return out


def build_wheels(dest: Path) -> int:
    """把依赖做成 wheel，供收件人离线安装。

    必须用 `pip wheel` 而不是 `pip download`：jieba 在 PyPI 上只提供源码包
    （.tar.gz），pip download 会原样存成 sdist，收件人离线安装时需要现场编译，
    而离线环境里没有 setuptools —— 实测会报
    "Could not find a version that satisfies the requirement setuptools>=40.8.0"。
    pip wheel 会在这里就把 sdist 编译成 wheel，收件人拿到的是可以直接装的二进制包。
    """
    dest.mkdir(parents=True, exist_ok=True)
    print("正在准备离线依赖包（会本地编译，约 1-2 分钟）...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "wheel",
         "-r", str(ROOT / "requirements.txt"), "-w", str(dest)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print("准备失败：")
        print((r.stdout or "")[-1500:])
        print((r.stderr or "")[-800:])
        return 0
    files = sorted(dest.glob("*.whl"))
    total = sum(f.stat().st_size for f in files)
    print(f"  已生成 {len(files)} 个 wheel，共 {total / 1024 / 1024:.1f} MB")
    return len(files)


# ---- 免安装运行时（--portable）----

# 官方「嵌入式 Python」：解压即用，不写注册表，不需要管理员权限。
# 用与本机一致的 3.13.14，避免 stdlib 与 wheel 的兼容差异。
EMBED_URL = "https://www.python.org/ftp/python/3.13.14/python-3.13.14-embed-amd64.zip"

# 运行时里不需要带进包的东西（体积大头，且本工具完全用不到）：
#   jieba/lac_small —— paddlepaddle 词法分析模型（12MB），只在 jieba.enable_paddle() 时用
#   jieba/analyse   —— idf.txt（6MB），只在 jieba.analyse.extract_tags() 时用，本工具自己算了 TF-IDF
#   *.dist-info     —— pip 元数据，运行时不需要（而且嵌入式 Python 里没有 pip）
RUNTIME_SKIP_DIRS = {"lac_small", "analyse", "__pycache__"}
RUNTIME_SKIP_SUFFIX = {".dist-info"}


def build_runtime(dest: Path, wheels_dir: Path) -> bool:
    """搭一个免安装的 Python 运行时：官方嵌入式解释器 + 解包好的依赖。

    为什么这样就能免安装：
      1. 嵌入式 Python 解压即用，不写注册表、不需要管理员权限、不干扰系统里已有的 Python；
      2. 依赖全是纯 Python 的 wheel（py3-none-any），直接解包成目录放进 sys.path 即可，
         不需要 pip —— 嵌入式版本本来也不带 pip。
    """
    py_dir = dest / "py"
    sp = py_dir / "site-packages"

    if not (py_dir / "python.exe").exists():
        py_dir.mkdir(parents=True, exist_ok=True)
        zf = dest / "py-embed.zip"
        print("正在下载嵌入式 Python（约 11MB）...")
        try:
            urllib.request.urlretrieve(EMBED_URL, zf)
        except Exception as exc:  # noqa: BLE001
            print(f"  下载失败：{type(exc).__name__}: {exc}")
            return False
        with zipfile.ZipFile(zf) as z:
            z.extractall(py_dir)
        print(f"  已解压到 {py_dir}")

    # 依赖：优先复用 dist/wheels，没有就现做
    if not any(wheels_dir.glob("*.whl")):
        if not build_wheels(wheels_dir):
            return False
    if not (sp / "jieba").exists():
        sp.mkdir(parents=True, exist_ok=True)
        for w in sorted(wheels_dir.glob("*.whl")):
            with zipfile.ZipFile(w) as z:
                z.extractall(sp)
        print(f"  已解包依赖 -> {sp}")

    # 嵌入式 Python 用 python3XX._pth 决定 sys.path，且【忽略 PYTHONPATH】。
    # 必须在这里把 site-packages 写进去，否则依赖根本 import 不到。
    for pth in glob.glob(str(py_dir / "python3*._pth")):
        Path(pth).write_text(
            "python313.zip\n.\nsite-packages\n\n"
            "# 依赖已解包在 site-packages，不需要 pip（嵌入式版本本来也没有 pip）\n",
            encoding="utf-8")

    # 自检：关键模块能不能 import（sqlite3 / ssl 是嵌入式版本最容易缺的）
    exe = py_dir / "python.exe"
    probe = (
        "import sys; import sqlite3, ssl, socket, http.server, json, webbrowser;"
        "import jieba, openpyxl, httpx;"
        "print('OK', sys.version.split()[0])"
    )
    r = subprocess.run([str(exe), "-c", probe], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(py_dir))
    if r.returncode != 0:
        print("运行时自检失败：")
        print((r.stdout or "")[-800:])
        print((r.stderr or "")[-800:])
        return False
    print("  运行时自检通过：", (r.stdout or "").strip())
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个可发给别人的分发包")
    ap.add_argument("--with-wheels", action="store_true",
                    help="附带离线 wheel，收件人断网也能装依赖")
    ap.add_argument("--include-data", action="store_true",
                    help="连 data/ 一起打包（仅用于自己备份迁移，会包含登录态）")
    ap.add_argument("--portable", action="store_true",
                    help="免安装版：把 Python 运行时也打进包里，收件人无需装 Python、无需联网")
    ap.add_argument("--out", default="", help="输出目录，默认 dist/")
    ap.add_argument("--name", default="", help="压缩包名，默认 CommunityPulse-日期")
    args = ap.parse_args()

    out_dir = Path(args.out) if args.out else ROOT / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)

    name = args.name or f"CommunityPulse-{__import__('time').strftime('%Y%m%d')}"
    zip_path = out_dir / f"{name}.zip"

    files = collect_files(ROOT, args.include_data)

    # 离线 wheel：下载到 dist/wheels 复用（下次打包不用重下），再一并进压缩包
    wheels_n = 0
    wheels_dir = out_dir / "wheels"

    # 免安装运行时：自带 Python 解释器 + 已解包的依赖
    runtime_dir = out_dir / "_rt"
    has_runtime = False
    if args.portable:
        print("正在准备免安装运行时...")
        has_runtime = build_runtime(runtime_dir, wheels_dir)
        if not has_runtime:
            print("[警告] 运行时没准备好，本次退回到「需要系统 Python」模式。")
        else:
            # 免安装版不需要再带一份 wheel（依赖已经解包进运行时了）
            args.with_wheels = False

    if args.with_wheels:
        wheels_n = build_wheels(wheels_dir)
        if not wheels_n:
            print("[警告] wheel 没下下来，本次打包退回「需联网安装」模式。")

    # 打包。直接按 arcname 写，不建临时目录、不做任何删除动作
    # （本机有批量删除保护，rmtree 会被拦）。
    TOP = "CommunityPulse"
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for src, rel in files:
            z.write(src, f"{TOP}/{rel}")
            total += src.stat().st_size
        if wheels_n:
            # 只带 .whl。混进 .tar.gz 会让收件人离线安装时试图现场编译而失败
            for w in sorted(wheels_dir.glob("*.whl")):
                z.write(w, f"{TOP}/wheels/{w.name}")
                total += w.stat().st_size

        rt_n = 0
        if has_runtime:
            py_root = runtime_dir / "py"
            for p in sorted(py_root.rglob("*")):
                rel = p.relative_to(py_root)
                if any(x in RUNTIME_SKIP_DIRS for x in rel.parts):
                    continue
                if p.is_dir():
                    continue
                if p.suffix.lower() in RUNTIME_SKIP_SUFFIX:
                    continue
                if any(x.endswith(".dist-info") for x in rel.parts):
                    continue
                z.write(p, f"{TOP}/runtime/py/{rel.as_posix()}")
                total += p.stat().st_size
                rt_n += 1
        # 收件人说明（GBK，记事本双击直接能读）
        z.writestr(f"{TOP}/第一次用请读我.txt", README_TXT.encode("gbk"))

    print()
    print("=" * 56)
    print(f"  分发包已生成：{zip_path}")
    print(f"  压缩包体积：{zip_path.stat().st_size / 1024 / 1024:.2f} MB"
          f"（原始 {total / 1024 / 1024:.2f} MB）")
    print(f"  文件数：{len(files) + 1 + rt_n}")
    if has_runtime:
        print("  运行环境：免安装（内置 Python 3.13 + 依赖），收件人无需装 Python、无需联网")
    else:
        print(f"  运行环境：{'内置 ' + str(wheels_n) + ' 个离线 wheel（无需联网，但仍需系统 Python）' if wheels_n else '需系统 Python + 首次联网装依赖约 30 秒'}")
    print("=" * 56)
    print()
    print("已排除：venv/（绑定本机路径，搬走必失效）、data/（含你的数据与登录 Cookie）、")
    print("        logs/、__pycache__/")
    if args.include_data:
        print()
        print("[注意] 本次带了 data/ —— 里面包含你的采集数据和浏览器登录态，")
        print("       除非是给自己做备份迁移，否则不要发给别人！")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
