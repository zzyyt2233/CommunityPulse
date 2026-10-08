"""启动日志落盘 + 按大小轮转。

双击启动.bat 时输出只打在黑窗口里，窗口一关就没了，出问题无从回看。
这里把 stdout / stderr 复制一份到 logs/app.log，控制台照常显示，不影响交互。

写满 5MB 自动归档：app.log -> app.1.log -> app.2.log，最多留 3 份，最老的丢掉。
归档前必须先关句柄——Windows 上文件被占用时改名直接失败。
"""
from __future__ import annotations

import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "logs"

MAX_BYTES = 5 * 1024 * 1024
KEEP = 3


def rotate(path, max_bytes: int = MAX_BYTES, keep: int = KEEP) -> None:
    """归档写满的日志。调用前该文件必须已经关闭。

    这里的异常一律吞掉，而且捕的是 BaseException 而不是 OSError ——
    受限环境（比如某些沙箱）拦下删除时抛的是 SystemExit，那玩意儿不是
    OSError 的子类，只捕 OSError 会让它直接穿出去，把整个服务带走。
    归档失败大不了不归档，继续往原文件写。
    """
    p = pathlib.Path(path)
    try:
        if not p.exists() or p.stat().st_size < max_bytes:
            return
    except BaseException:            # noqa: BLE001
        return

    stem, suffix = p.stem, p.suffix
    try:
        oldest = p.with_name(f"{stem}.{keep}{suffix}")
        if oldest.exists():
            oldest.unlink()
    except BaseException:            # noqa: BLE001
        return                       # 连最老的都删不掉，后面几步必然也做不成

    for i in range(keep - 1, 0, -1):
        src = p.with_name(f"{stem}.{i}{suffix}")
        if not src.exists():
            continue
        try:
            src.replace(p.with_name(f"{stem}.{i + 1}{suffix}"))
        except BaseException:        # noqa: BLE001
            pass

    try:
        p.replace(p.with_name(f"{stem}.1{suffix}"))
    except BaseException:            # noqa: BLE001
        pass


class _Sink:
    """日志文件本体。stdout / stderr 共用一份，免得两个句柄各自轮转打架。"""

    def __init__(self, path):
        self._path = pathlib.Path(path)
        self._fh = None
        self._written = 0
        self._open()

    def _open(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            rotate(self._path, MAX_BYTES, KEEP)
            # 行缓冲：用户点窗口右上角关掉时是强杀，进程内的块缓冲会整个丢掉，
            # 那日志就白记了。每行都落盘，本地工具这点开销无所谓。
            self._fh = io.open(self._path, "a", encoding="utf-8",
                               errors="replace", buffering=1)
        except OSError:
            self._fh = None          # 写不了日志不该拖垮服务
        self._written = 0

    def write(self, s: str) -> None:
        if self._fh is None or not s:
            return
        try:
            self._fh.write(s)
            self._written += len(s)
            if self._written >= MAX_BYTES:
                self._reopen()
        except Exception:            # noqa: BLE001
            pass

    def flush(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
        except Exception:            # noqa: BLE001
            pass

    def _reopen(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:            # noqa: BLE001
            pass
        self._open()                 # 里面会先把写满的那份归档


class _Tee:
    """写往控制台的同时复制一份进日志文件。"""

    def __init__(self, console, sink: _Sink):
        self._console = console
        self._sink = sink

    def write(self, s: str) -> int:
        if not s:
            return 0
        if self._console is not None:
            try:
                self._console.write(s)
            except Exception:        # noqa: BLE001
                pass
        self._sink.write(s)
        return len(s)

    def flush(self) -> None:
        if self._console is not None:
            try:
                self._console.flush()
            except Exception:        # noqa: BLE001
                pass
        self._sink.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        # encoding / errors / fileno 这类属性转给原来的 console。
        # 私有名直接抛，否则属性没设好时会自己递进自己。
        if name.startswith("_"):
            raise AttributeError(name)
        console = self.__dict__.get("_console")
        if console is None:
            raise AttributeError(name)
        return getattr(console, name)


_installed = False


def install(path=None):
    """把 stdout / stderr 复制一份到日志文件。重复调用只生效一次。

    返回实际使用的日志路径；装不上返回 None（不影响服务启动）。
    """
    global _installed
    if _installed:
        return None
    target = pathlib.Path(path) if path else (LOG_DIR / "app.log")
    try:
        sink = _Sink(target)
        sys.stdout = _Tee(sys.stdout, sink)
        sys.stderr = _Tee(sys.stderr, sink)
        _installed = True
        return target
    except Exception:                # noqa: BLE001
        return None
