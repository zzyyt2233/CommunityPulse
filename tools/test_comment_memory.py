# -*- coding: utf-8 -*-
"""离线验证：语句 / 评论级情感记忆（改标签 → 记忆 → 下次正确标记）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.analysis import comment_memory as cm  # noqa: E402
from app.analysis import sentiment_memory as sm  # noqa: E402
from app.analysis.pipeline import analyze  # noqa: E402

# 干净起点，避免历史记忆干扰（也保证不污染用户数据）
cm.MEMORY_FILE.unlink(missing_ok=True)
sm.MEMORY_FILE.unlink(missing_ok=True)

COMMENTS = [
    {"id": 1, "content": "优化太差了，一直闪退", "like_count": 30, "user_name": "a"},
    {"id": 2, "content": "这游戏优化太差，服务器还炸了", "like_count": 20, "user_name": "b"},
    {"id": 3, "content": "画面精美但是优化拉胯", "like_count": 10, "user_name": "c"},
    {"id": 4, "content": "客服态度很好，处理很快", "like_count": 8, "user_name": "d"},
    {"id": 5, "content": "这游戏真不错，剧情很棒", "like_count": 6, "user_name": "e"},
    {"id": 6, "content": "优化太差", "like_count": 5, "user_name": "f"},
]

checks = []


def check(name, ok):
    checks.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name)


print("== 1) 首次分析（尚无记忆） ==")
r1 = analyze(COMMENTS, {"top_n": 20})
print("  词条:", [(k["word"], k["kind"]) for k in r1["keywords"][:6]])
print("  分布:", {k: r1["stats"][k] for k in ("total", "positive", "neutral", "negative", "memory_marked")})
check("词条里出现短语", any(k["kind"] == "phrase" for k in r1["keywords"]))
check("短语排在单词之前", r1["keywords"][0]["kind"] == "phrase")
check("初始无记忆命中", r1["stats"]["memory_marked"] == 0)
check("吐槽被自动判为负面（>=3 条）", r1["stats"]["negative"] >= 3)

print("\n== 2) 把一条差评人工标成正面，重新分析 ==")
TARGET = COMMENTS[0]["content"]
key = cm.norm_key(TARGET)
cm.save(key=key, label="positive", text=TARGET)
r2 = analyze(COMMENTS, {"top_n": 20})
print("  分布:", {k: r2["stats"][k] for k in ("positive", "neutral", "negative", "memory_marked")})
check("记忆命中 1 条", r2["stats"]["memory_marked"] == 1)
check("负面数 -1", r2["stats"]["negative"] == r1["stats"]["negative"] - 1)
check("正面数 +1", r2["stats"]["positive"] == r1["stats"]["positive"] + 1)

pos = r2["sentiment_examples"]["positive"]
hit = [x for x in pos if x.get("key") == key]
check("该条进入「最正面原声」", bool(hit))
check("原声带 mem_label=positive", bool(hit) and hit[0].get("mem_label") == "positive")
check("得分与标签同号（正）", bool(hit) and float(hit[0]["score"]) >= 0)

print("\n== 3) 归一化：标点 / 空格 / 表情 不影响命中 ==")
check("指纹对表情与空格鲁棒",
      cm.norm_key("优化太差了，一直闪退") == cm.norm_key("优化太差了  一直闪退😀"))
check("按正文查标签命中", cm.get_label("优化太差了 一直闪退 [doge]") == "positive")

print("\n== 4) 跨来源 / 跨任务：同内容再出现时自动沿用 ==")
other = [{"id": 999, "content": "优化太差了，一直闪退", "like_count": 1, "user_name": "z"}]
r3 = analyze(other, {"top_n": 10})
check("新任务里同内容命中记忆",
      r3["stats"]["memory_marked"] == 1 and r3["stats"]["positive"] == 1)

print("\n== 5) 撤销记忆后恢复自动判定 ==")
cm.remove(key)
r4 = analyze(COMMENTS, {"top_n": 20})
check("撤销后不再命中", r4["stats"]["memory_marked"] == 0)
check("撤销后回到自动判定", r4["stats"]["negative"] == r1["stats"]["negative"])

# 清理测试残留
cm.MEMORY_FILE.unlink(missing_ok=True)
sm.MEMORY_FILE.unlink(missing_ok=True)

bad = [n for n, ok in checks if not ok]
print("\n" + ("ALL PASS" if not bad else f"FAILED: {bad}"))
sys.exit(1 if bad else 0)
