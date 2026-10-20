"""把一次分析结果导成 PulseBoard 站点的只读快照。

只读 data\\pulse.db，不写库、不联网、不碰 Cookie 与 API Key。
默认导出最近一个「有分析结果」的任务，评论者用户名一律去掉。

用法：
    venv\\Scripts\\python.exe tools\\export_site.py                # 最近一个有结果的任务
    venv\\Scripts\\python.exe tools\\export_site.py 699            # 指定任务
    venv\\Scripts\\python.exe tools\\export_site.py 699 --keep-user
    venv\\Scripts\\python.exe tools\\export_site.py --out D:\\x\\pulse.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402

# 站点工程与本仓库同级：E:\CommunityPulse → E:\PulseBoard
DEFAULT_OUT = ROOT.parent / "PulseBoard" / "web" / "data" / "pulse.json"
# 用户名是采集来的账号标识，站内展示不需要；key/mem_label 是本地记忆功能的内部字段
DROP_KEYS = {"user", "user_name", "key", "mem_label"}


def scrub(o):
    """递归丢掉 DROP_KEYS，其余字段原样带出去。"""
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k not in DROP_KEYS}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def pick_task(want: int | None) -> dict | None:
    if want:
        t = db.get_task(want)
        if not t:
            sys.exit(f"任务 #{want} 不存在")
        return t
    for t in db.list_tasks(limit=100):
        if db.get_latest_analysis(t["id"]):
            return t
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="导出 PulseBoard 舆情快照")
    ap.add_argument("task", nargs="?", type=int, help="任务 id，默认取最近一个有分析结果的")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSON 路径")
    ap.add_argument("--keep-user", action="store_true",
                    help="保留评论者用户名（默认去掉，发布到公网前请三思）")
    a = ap.parse_args()

    t = pick_task(a.task)
    if not t:
        sys.exit("库里没有任何带分析结果的任务，先在界面上跑一次分析。")
    an = db.get_latest_analysis(t["id"])
    if not an or not isinstance(an.get("result"), dict):
        sys.exit(f"任务 #{t['id']} 的分析结果读不出来（可能只有采集没有分析）。")

    result = an["result"] if a.keep_user else scrub(an["result"])
    stats = result.get("stats", {})
    payload = {
        "meta": {
            "generated_at": time.time(),
            "exported_by": "CommunityPulse tools/export_site.py",
            "anonymized": not a.keep_user,
            "task_id": t["id"],
            "task_name": t.get("name", ""),
            "platform": t.get("platform", ""),
            "source_url": t.get("source_url", ""),
            "source_title": t.get("source_title", ""),
            "comment_total": db.count_comments(t["id"]),
            "analyzed_at": an.get("created_at"),
            "params": an.get("params", {}),
        },
        "result": result,
    }

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    n_kw = len(result.get("keywords", []))
    n_cl = len(result.get("clusters", []))
    n_hl = len(result.get("highlights", []))
    print(f"任务 #{t['id']} {t.get('name', '')}：{stats.get('total', 0)} 条有效评论，"
          f"{n_kw} 个词条、{n_cl} 个族群、{n_hl} 条重点原句")
    print(f"已写出 {a.out}（{a.out.stat().st_size // 1024} KB，"
          f"{'已去用户名' if not a.keep_user else '保留了用户名'}）")


if __name__ == "__main__":
    main()
