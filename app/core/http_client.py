"""统一 HTTP 客户端：优先 httpx，缺失时降级到标准库 urllib。

对外只暴露 get_json / get_text，带 UA、超时、重试与简单节流。
"""
from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.request
import zlib
from typing import Any

from .config import DEFAULT_HEADERS, PAGE_DELAY, REQUEST_RETRY, REQUEST_TIMEOUT

try:  # 可选依赖
    import httpx  # type: ignore
except Exception:  # pragma: no cover
    httpx = None  # type: ignore


_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    delta = time.time() - _last_request_at
    if _last_request_at and delta < PAGE_DELAY:
        time.sleep(PAGE_DELAY - delta + random.uniform(0, 0.15))
    _last_request_at = time.time()


def _decode(raw: bytes, headers: Any) -> str:
    enc = ""
    try:
        enc = (headers.get("Content-Encoding") or "").lower()
    except Exception:
        enc = ""
    if "gzip" in enc:
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    for enc_name in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc_name)
        except Exception:
            continue
    return raw.decode("utf-8", errors="ignore")


def _urllib_get(url: str, headers: dict, timeout: float) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), _decode(raw, resp.headers)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = _decode(e.read(), e.headers)
        except Exception:
            pass
        return e.code, dict(e.headers or {}), body


def request(url: str, headers: dict | None = None, timeout: float = REQUEST_TIMEOUT,
            retries: int = REQUEST_RETRY, json_mode: bool = False) -> Any:
    """返回文本或解析后的 JSON；连续失败抛最后一次异常。"""
    hdrs = dict(DEFAULT_HEADERS)
    if headers:
        hdrs.update(headers)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        _throttle()
        try:
            if httpx is not None:
                with httpx.Client(follow_redirects=True, timeout=timeout, headers=hdrs) as cli:
                    resp = cli.get(url)
                    text = resp.text
                    status = resp.status_code
            else:
                status, _, text = _urllib_get(url, hdrs, timeout)
            if status >= 400:
                raise RuntimeError(f"HTTP {status} for {url}")
            return json.loads(text) if json_mode else text
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"请求失败: {url} -> {last_err}")


def get_json(url: str, headers: dict | None = None, **kw: Any) -> Any:
    return request(url, headers, json_mode=True, **kw)


def get_text(url: str, headers: dict | None = None, **kw: Any) -> str:
    return request(url, headers, json_mode=False, **kw)


def post_json(url: str, payload: dict, headers: dict | None = None,
              timeout: float = REQUEST_TIMEOUT) -> Any:
    """POST JSON（小红书等签名接口用）。httpx 可用时走 httpx，否则用 urllib。"""
    hdrs = dict(DEFAULT_HEADERS)
    hdrs["Content-Type"] = "application/json;charset=UTF-8"
    if headers:
        hdrs.update(headers)
    _throttle()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if httpx is not None:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers=hdrs) as cli:
            resp = cli.post(url, content=body)
            text = resp.text
    else:
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = _decode(r.read(), r.headers)
    return json.loads(text)
