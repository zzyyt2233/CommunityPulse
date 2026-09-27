"""冒烟测试：分析链路 + 导入解析 + 导出 + 真实接口采集。"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import db  # noqa: E402
from app.analysis import HAS_JIEBA, analyze  # noqa: E402
from app.collectors import parse_import, route  # noqa: E402
from app.export import HAS_OPENPYXL, to_csv, to_excel, to_html_report  # noqa: E402

SAMPLES = [
    ("这版本优化太差了，我的手机一进副本就掉帧", 128),
    ("优化太差了，一打团就掉帧卡顿，什么时候修", 96),
    ("优化真的差，中低端机根本跑不动，发烫严重", 74),
    ("抽卡爆率一如既往的感人，80抽了还没出，骗氪", 210),
    ("爆率太低了，氪了两单还是没出货，吃相难看", 155),
    ("这爆率真的离谱，零氪玩家完全没法玩", 88),
    ("648 太贵了，性价比极低，劝退新人", 143),
    ("月卡性价比还不错，微氪玩家能接受", 61),
    ("剧情写得很好，主线结局有点仓促但整体满意", 88),
    ("这剧情真不错，配音也很到位，好评", 72),
    ("立绘很精美，但建模和立绘差距有点大", 55),
    ("建模崩了，立绘和建模完全不是一个人", 47),
    ("匹配机制太烂了，连败之后给我匹配摆烂队友", 176),
    ("排位匹配机制真的烂，队友挂机也不管", 122),
    ("外挂太多了，举报了也没用，官方装死", 165),
    ("外挂横行，封号力度不够，建议加大检测", 98),
    ("新手引导太啰嗦，萌新看完还是不知道干嘛", 66),
    ("新手引导可以再简化一点，太长了", 41),
    ("客服回应很快，问题当天就解决了，点赞", 52),
    ("客服装死，工单三天没人回，冷处理玩家", 118),
    ("这次补偿挺良心的，官方有诚意", 91),
    ("补偿太少了，就这点东西打发谁呢", 103),
    ("内容太少了，长草期两个月没新活动", 87),
    ("每天上线就是日常周常，跟上班一样，太肝了", 112),
    ("日常太肝了，爆肝两小时做不完，劝退", 95),
    ("打击感很棒，连招手感非常爽，好评", 134),
    ("手感不错，操作也流畅，玩起来很舒服", 78),
    ("UI 界面改版之后反而更难用了，按钮太小", 63),
    ("服务器又炸了，维护了六个小时还没开", 141),
    ("闪退太频繁了，每次切后台就崩，修一下吧", 109),
    ("游戏整体还行，就是希望优化能做好", 45),
    ("玩了三年了，还是一如既往的好玩", 58),
    ("这次更新之后确实流畅了很多，进步明显", 82),
    ("平衡性调整还行，削弱得没那么离谱", 44),
    ("联动活动挺有意思的，期待下次", 39),
    ("联机组队经常匹配不到人，等太久", 57),
]

PASTE = """玩家A：这版本优化太差了，掉帧严重
玩家B：爆率太低了，氪了两单没出货
玩家C：剧情不错，配音到位
玩家D：匹配机制太烂了"""

CSV_TEXT = """user,content,like,time
小明,优化太差了，掉帧卡顿,120,2026-09-01 10:00
小红,爆率感人，骗氪,80,2026-09-02 11:00
小刚,剧情很好，好评,60,2026-09-03 12:00"""

JSON_TEXT = '[{"content":"外挂太多了，官方装死","like":99},{"content":"客服回应很快","like":20}]'


def main() -> int:
    print(f"[env] jieba={HAS_JIEBA} openpyxl={HAS_OPENPYXL}")
    db.init_db()

    # 1) 导入解析
    for name, raw, fmt in (("text", PASTE, "text"), ("csv", CSV_TEXT, "csv"), ("json", JSON_TEXT, "json")):
        rows = parse_import(raw, fmt)
        print(f"[import] {name}: {len(rows)} 条 -> {[r['content'][:18] for r in rows][:2]}")
        assert rows, f"{name} 解析为空"

    # 2) 分析链路
    comments = [{"content": c, "like_count": l, "user_name": f"user{i}",
                 "published_at": time.time() - (i % 10) * 86400}
                for i, (c, l) in enumerate(SAMPLES * 3)]
    t0 = time.time()
    res = analyze(comments, {"top_n": 40, "cluster_threshold": 0.55})
    cost = time.time() - t0
    st = res["stats"]
    print(f"[analyze] {st['total']} 条 / 合并重复 {st['duplicates_merged']} / 耗时 {cost:.2f}s")
    print(f"          情感: 正 {st['positive']} 中 {st['neutral']} 负 {st['negative']} "
          f"差评率 {st['negative_rate']:.0%}")
    print(f"          词条 Top8: {[k['word'] for k in res['keywords'][:8]]}")
    print(f"          相似句族群 {len(res['clusters'])} 个，最大簇 {res['clusters'][0]['size']} 条")
    print(f"          吐槽点: {[(t['topic'], t['count']) for t in res['topics'][:5]]}")
    print(f"          趋势点: {len(res['trend'].get('points', []))}，重点原句 {len(res['highlights'])} 条")
    assert st["total"] > 0 and res["keywords"] and res["clusters"]
    assert res["clusters"][0]["size"] >= 2, "相似句聚类未生效：最大簇应 >= 2 条"

    big = res["clusters"][0]
    print(f"          示例簇: [{big['size']}条] {big['representative'][:40]}... 关键词 {big['keywords']}")

    # 3) 导出
    p1 = to_html_report("冒烟测试", res, comments)
    print(f"[export] html -> {p1.name} ({p1.stat().st_size // 1024} KB)")
    p2 = to_csv("冒烟测试", res, comments)
    print(f"[export] csv  -> {p2.name} ({p2.stat().st_size // 1024} KB)")
    if HAS_OPENPYXL:
        p3 = to_excel("冒烟测试", res, comments)
        print(f"[export] xlsx -> {p3.name} ({p3.stat().st_size // 1024} KB)")

    # 4) 真实接口（Steam 公开评测）
    url = "https://store.steampowered.com/app/570"
    c = route(url)
    print(f"[live] route {url} -> {c.platform}({c.reliability})")
    try:
        r = c.collect(url, limit=40)
        print(f"[live] steam 抓到 {len(r.comments)} 条；示例：{r.comments[0].content[:40] if r.comments else '无'}")
    except Exception as exc:  # noqa: BLE001
        print(f"[live] steam 抓取失败（不影响本地功能）: {type(exc).__name__}: {exc}")

    print("\nOK: 冒烟测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
