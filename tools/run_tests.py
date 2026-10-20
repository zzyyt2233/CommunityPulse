"""一次跑完所有离线测试套件。

tools/ 下这些 test_*.py 原本各跑各的，改完一处想确认没弄坏别处只能一个个敲。
CI 也需要一个明确的入口，不然「跑测试」就成了不知道跑什么。

把需要服务在跑的几个（ui_check / xss_check / live_test / http_probe）排除在外 ——
它们不是离线套件，硬塞进来会让结果取决于当时有没有开服务。

用法：
    venv\\Scripts\\python.exe tools/run_tests.py           # 全跑
    venv\\Scripts\\python.exe tools/run_tests.py --quick   # 跳过启动真线程/真浏览器的那两套
    venv\\Scripts\\python.exe tools\\run_tests.py test_negative_alert test_collect_gate
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (脚本名, 是否慢)
# 顺序有讲究：test_logsetup 要删掉最老的那份归档日志，而受限环境（沙箱、
# 某些企业策略）的删除额度是按会话累计的，排到后面会被前面的套件用光。
SUITES: list[tuple[str, bool]] = [
    ("test_logsetup", False),
    # 放前面：它做的是并发写库的验证，跑在后面容易被别的套件留下的锁状态干扰
    ("test_db_concurrency", False),
    ("test_secret_settings", False),
    ("test_negative_alert", False),
    ("test_comment_memory", False),
    ("test_sentiment_memory", False),
    ("test_phrase_extract", False),
    ("test_browser_merge", False),
    ("test_discovery_search", False),
    ("test_browser_capture", False),
    ("test_autocapture", True),      # 真起线程跑自动捕获
    ("test_collect_gate", True),     # 真起线程验并发闸门排队
    # 预警中心：面板是死的、接口是活的，两边字段都要验。起真服务走 HTTP。
    ("test_alert_center", True),
    # 监测清单：db 的 CRUD + 增量采集 + 分析 + 预警这条接线，最后一层起真服务。
    ("test_watchlist", True),
    # 全链路冒烟：分析 + 导入解析 + 导出三格式，最后一步真连 Steam 抓一次
    # （失败只打印不判败），所以整体按离线套件对待。以前一直没被收进来，
    # 只在本地手动跑 —— 是个漏网的孤儿。
    ("smoke_test", False),
    # 启动/停止 .bat 与 make_bat 的一致性（防「手改 .bat 不改生成器」分叉）
    # 和 GBK+CRLF 编码规则。只读比对，零删除额度消耗。
    ("test_make_bat", False),
    # 停止服务的解析与防御逻辑：端口精确匹配（:87660 不误配 :8766）、
    # 畸形 netstat/lsof 输出不炸。不碰真进程。
    ("test_stop_instance", False),
    # 分发包的排除规则：data/ 里有采集数据和浏览器登录态，默认绝不能进包。
    # 纯路径判断、造临时目录即跑即删，秒级。此前这条安全承诺没测试守着。
    ("test_make_dist", False),
]


# 摘要行里不想要的噪音：jieba 建词典是 stderr 上的正常输出，
# safe-delete 是受限环境的删除守卫日志（测试仍然通过，只是看着吓人）。
_NOISE = ("prefix dict", "building prefix dict", "loading model",
          "[safe-delete]", "dumping model")


def run_one(name: str, timeout: int = 300) -> tuple[bool, str, float]:
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, os.path.join("tools", name + ".py")],
                           cwd=ROOT, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"超时（>{timeout}s）", time.time() - t0
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace")
    merged = out + "\n" + err
    tail = ""
    for line in reversed(merged.strip().splitlines()):
        low = line.strip().lower()
        if not low or any(n in low for n in _NOISE):
            continue
        tail = line.strip()
        break
    return r.returncode == 0, tail, time.time() - t0


def main() -> int:
    args = sys.argv[1:]
    quick = "--quick" in args
    picked = [a for a in args if not a.startswith("--")]
    py = os.path.basename(sys.executable)

    print("=" * 72)
    print(f"CommunityPulse 离线测试（解释器 {py}）")
    print("=" * 72)

    results: list[tuple[str, bool, float, str]] = []
    for name, slow in SUITES:
        if picked and name not in picked:
            continue
        if slow and quick:
            print(f"{name:<24} 跳过（--quick）")
            continue
        ok, tail, dt = run_one(name)
        results.append((name, ok, dt, tail))
        print(f"{name:<24} {'PASS' if ok else 'FAIL'}  {dt:5.1f}s  {tail[:70]}")

    bad = [r for r in results if not r[1]]
    print("\n" + "=" * 72)
    if bad:
        print(f"失败 {len(bad)} / {len(results)} 套：")
        for name, _, _, tail in bad:
            print(f"   - {name}: {tail[:140]}")
        return 1
    print(f"全部 {len(results)} 套离线测试通过 ✅")
    print("以下需要服务在跑，不在这里，另起服务后手动跑：")
    print("   http_probe   venv\\Scripts\\python.exe tools\\http_probe.py 8801"
          "   HTTP 端点全量巡检")
    print("   ui_check     同上，但会开无头浏览器截图")
    print("   xss_check    同上，带XSS 载荷")
    print("   live_test    真连平台抓取，需要网络与登录态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
