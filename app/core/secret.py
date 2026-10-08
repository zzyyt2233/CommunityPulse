"""凭据加密：用 Windows 自带的 DPAPI 把 Cookie / API Key 加密后再落盘。

为什么用 DPAPI 而不是自己造：密钥由操作系统按「当前 Windows 用户」托管，
加解密都不需要自己管密钥存哪、怎么保护，也不用引第三方库（直接 ctypes 调 crypt32）。

代价要说清楚：密文**只有同一台机器的同一个 Windows 账户**能解开。
换机器、换用户、把 data/settings.json 拷给别人 —— 解不开，当作「没配过」，
让用户重新登录一次就好。

非 Windows 平台不加密（原样存），这个工具本来就只在 Windows 上跑。
"""
from __future__ import annotations

import base64
import ctypes
import sys

PREFIX = "dpapi:v1:"          # 带这个前缀的才当密文看，没前缀的一律当明文（兼容老文件）
_IS_WINDOWS = sys.platform == "win32"

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


if _IS_WINDOWS:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(_Blob)]
    _crypt32.CryptProtectData.restype = ctypes.c_int

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_Blob), ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.POINTER(_Blob)]
    _crypt32.CryptUnprotectData.restype = ctypes.c_int

    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def _to_blob(data: bytes):
    """bytes -> DATA_BLOB。返回值里的 buf 必须活到 API 调用结束，否则内存被回收。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _from_blob(blob: _Blob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def is_encrypted(value) -> bool:
    """是不是本模块写出来的密文。

    故意不要求 str：settings.json 是手也能改的 JSON，值不保证是字符串
    （把 token 填成数字、填成数组都可能），这里只要不是字符串就一律当明文。
    """
    return isinstance(value, str) and value.startswith(PREFIX)


def protect(text):
    """明文 -> 带前缀的密文。空值原样返回；加密失败也退回明文（别把数据弄丢）。"""
    if not isinstance(text, str) or not text or not _IS_WINDOWS:
        return text
    src, _keep = _to_blob(text.encode("utf-8"))
    out = _Blob()
    ok = _crypt32.CryptProtectData(ctypes.byref(src), None, None, None, None,
                                   _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out))
    if not ok:
        return text
    try:
        enc = _from_blob(out)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    return PREFIX + base64.b64encode(enc).decode("ascii")


def unprotect(token):
    """密文 -> 明文。没前缀的当明文（兼容加密之前存的老文件）。

    解不开（换机器 / 换了 Windows 账户 / 文件被改过）返回空串 ——
    对调用方来说等同于「这项没配过」，重新登录一次即可。
    非字符串的值（数字、布尔、结构）原样返回：那不是本模块写的东西。
    """
    if not isinstance(token, str) or not token or not is_encrypted(token):
        return token
    if not _IS_WINDOWS:
        return ""
    try:
        raw = base64.b64decode(token[len(PREFIX):])
    except Exception:            # noqa: BLE001
        return ""
    src, _keep = _to_blob(raw)
    out = _Blob()
    desc = ctypes.c_wchar_p()
    ok = _crypt32.CryptUnprotectData(ctypes.byref(src), ctypes.byref(desc), None,
                                     None, None, _CRYPTPROTECT_UI_FORBIDDEN,
                                     ctypes.byref(out))
    if not ok:
        return ""
    try:
        plain = _from_blob(out)
    finally:
        if desc:
            _kernel32.LocalFree(ctypes.cast(desc, ctypes.c_void_p))
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    return plain.decode("utf-8", "replace")
