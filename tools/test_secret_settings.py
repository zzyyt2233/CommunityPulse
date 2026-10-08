# -*- coding: utf-8 -*-
"""离线验证：凭据加密落盘（app/core/secret.py + app/core/config.py）。

覆盖：加解密往返、盘上确实是密文、非凭据字段原样存、嵌套结构也覆盖、
兼容老的明文 settings、密文解不开时的提示。

整个测试把 config.DATA_DIR 指到 temp 目录，绝不碰真实的 data/settings.json。
"""
import io
import json
import pathlib
import shutil
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.core import config, secret  # noqa: E402

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


def main():
    for s in ("", "SESSDATA=abc; bili_jct=def", "AIzaSy-带中文-测试", "x" * 300):
        ok(f"往返一致: {s[:18]!r}", secret.unprotect(secret.protect(s)) == s)
    ok("空串不加密", secret.protect("") == "")
    ok("没前缀的当明文", secret.unprotect("plain-cookie") == "plain-cookie")
    # settings.json 可以手改，值不保证是字符串。非字符串一律当明文原样放行，
    # 不能在读配置这一步抛 AttributeError，把整个平台接入列表弄没。
    for v in (25, 0, True, None, ["a"], {"k": "v"}):
        ok(f"非字符串 {v!r} 原样穿过（不加密）", secret.protect(v) == v)
        ok(f"非字符串 {v!r} 原样返回（不解密）", secret.unprotect(v) == v)
        ok(f"非字符串 {v!r} 不算密文", secret.is_encrypted(v) is False)

    d = ROOT / "temp" / "secret_settings"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    orig_dir = config.DATA_DIR
    config.DATA_DIR = d
    try:
        f = d / "settings.json"
        config.save_settings({
            "bilibili_cookie": "SESSDATA=secret-value; x=y",
            "weibo_cookie": "SUB=another-secret",
            "youtube_api_key": "AIzaSy-fake-key-123",
            "threshold": 25,                 # 非凭据：原样存，方便排查
            "sort": "hot",
            "nested": {"proxy_cookie": "inside-secret"},
        })
        raw = f.read_text(encoding="utf-8")
        s = config.load_settings()
        ok("读回来是明文，采集器能直接用",
           s["bilibili_cookie"] == "SESSDATA=secret-value; x=y"
           and s["youtube_api_key"] == "AIzaSy-fake-key-123")
        ok("嵌套的凭据也解回来了", s["nested"]["proxy_cookie"] == "inside-secret")
        ok("非凭据字段原样返回", s["threshold"] == 25 and s["sort"] == "hot")
        ok("没有解不开的项时不报丢失", config.lost_secrets() == [])

        # 落盘形态只有 Windows 上才有「必须是密文」这条约束：DPAPI 是系统自带的，
        # 其它平台 secret.protect 直接原样返回，所以这里按平台分别断言，
        # 而不是把整组跳过 —— 跳过一个平台就等于那个平台的 CI 门禁是空的。
        if secret._IS_WINDOWS:
            ok("盘上看不到 Cookie 明文", "secret-value" not in raw)
            ok("盘上看不到 API Key 明文", "AIzaSy-fake-key-123" not in raw)
            ok("嵌套结构里的凭据也加密了", "inside-secret" not in raw)
            ok("盘上确实带密文前缀", raw.count(secret.PREFIX) >= 4)
            ok("非凭据字段照旧是明文（好排查）",
               '"threshold": 25' in raw and '"hot"' in raw)
        else:
            # 非 Windows：明文落盘是既定行为，但「读回来能用」不能因此打折。
            ok("非 Windows 平台不加密（原样落盘）", "secret-value" in raw)
            ok("非 Windows 平台也不加前缀", secret.PREFIX not in raw)
            ok("非 Windows 平台读回来仍是完整明文",
               s["bilibili_cookie"] == "SESSDATA=secret-value; x=y")
            ok("非 Windows 平台嵌套凭据也完整",
               s["nested"]["proxy_cookie"] == "inside-secret")
            ok("非 Windows 平台非凭据字段仍照旧",
               '"threshold": 25' in raw and '"sort": "hot"' in raw)

        # 兼容加密之前存的明文 settings
        f.write_text(json.dumps({"bilibili_cookie": "OLD-PLAIN-COOKIE"}),
                     encoding="utf-8")
        s2 = config.load_settings()
        ok("老的明文 settings 照样能读", s2["bilibili_cookie"] == "OLD-PLAIN-COOKIE")

        # 密文解不开（换机器 / 换 Windows 账户）
        f.write_text(json.dumps({
            "bilibili_cookie": secret.PREFIX + "bm90LWEtcmVhbC1ibG9i",
            "sort": "hot",
        }), encoding="utf-8")
        s3 = config.load_settings()
        ok("解不开的凭据变成空（等同没配过）", s3["bilibili_cookie"] == "")
        ok("解不开的字段名被记下来，启动时会提示",
           "bilibili_cookie" in config.lost_secrets())
    finally:
        config.DATA_DIR = orig_dir
        shutil.rmtree(d, ignore_errors=True)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
