"""离线验证：表情/贴纸过滤 + 情感记忆覆盖 + 词条标注。"""
import sys, json
sys.path.insert(0, '.')

from app.analysis.text_utils import tokenize, discover_new_words, is_emoji_or_sticker
from app.analysis.sentiment_memory import set_label, load_memory, overrides_to_lexicon, MEMORY_FILE
from app.analysis.sentiment import score_text
from app.analysis.pipeline import analyze

fail = 0
def check(name, cond):
    global fail
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        fail += 1

# ---------- 1) 表情 / 贴纸过滤 ----------
emoji_cases = ["[赞]", "[doge]", "[滑稽]", "[吃瓜]", "😀", "👍🔥", ":smile:", "orz", "T_T", "QAQ"]
for t in emoji_cases:
    check(f"is_emoji_or_sticker({t!r})", is_emoji_or_sticker(t))

txt = "这游戏太肝了[滑稽] 优化真差😀 但是美术精美👍 一般般吧 [doge]"
toks = tokenize(txt)
print("  tokenize ->", toks)
check("表情贴纸不应进入 token", not any(is_emoji_or_sticker(w) for w in toks))
check("正常词保留(太肝/优化)", "太肝" in toks and "优化" in toks)
check("含表情词剥离后保留中文(精美)", "精美" in toks)
check("贴纸名未残留(滑稽)", "滑稽" not in toks)
check("短码已移除([doge])", "doge" not in toks)

# 新词发现不应含表情
new_w = discover_new_words(["这游戏卡顿😀太肝了[doge]优化差😀画面赞👍" * 5], top_n=20)
print("  new_words ->", [n["word"] for n in new_w])
check("新词发现不含表情", not any(is_emoji_or_sticker(n["word"]) or "😀" in n["word"] for n in new_w))

# ---------- 2) 记忆覆盖打分 ----------
MEMORY_FILE.unlink(missing_ok=True)
set_label("卡顿", "positive")    # 覆盖：默认是负面(-2.5)，改判正面
set_label("优化", "negative")    # 覆盖：演示改判负面
set_label("好玩", "neutral")     # 覆盖：默认是正面(2)，改判中立(忽略)
mem = load_memory()
ov = overrides_to_lexicon(mem)
print("  memory ->", json.dumps(mem, ensure_ascii=False))
print("  overrides ->", {k: (list(v.keys()) if isinstance(v, dict) else v) for k, v in ov.items()})

lab_pos, _, _ = score_text("游戏卡顿但是能玩", ov)     # 卡顿被改判正面
lab_neg, _, _ = score_text("优化太差了一直闪退", ov)    # 优化被改判负面
lab_neu, _, _ = score_text("这游戏好玩", ov)            # 好玩被忽略 -> 无情感词 -> neutral
print("  卡顿(记忆正面) ->", lab_pos)
print("  优化(记忆负面) ->", lab_neg)
print("  好玩(记忆中立) ->", lab_neu)
check("记忆：卡顿被改判正面(覆盖默认负面)", lab_pos == "positive")
check("记忆：优化被改判负面", lab_neg == "negative")
check("记忆：好玩被忽略为中立", lab_neu == "neutral")

# ---------- 3) 流水线标注 ----------
MEMORY_FILE.unlink(missing_ok=True)
set_label("优化", "negative")
comments = []
base = [
    "优化太差了一直闪退😀 不想玩了",
    "画面精美[赞]但是优化拉胯 退游",
    "优化差到离谱[滑稽] 服务器还炸",
    "剧情不错就是优化一般般",
    "优化问题最多 其他还行",
]
for b in base:
    comments.append({"content": b, "like_count": 5})
res = analyze(comments, {"top_n": 30})
kws = {k["word"]: k for k in res["keywords"]}
print("  关键词 ->", [(k["word"], k.get("auto_label"), k.get("memory_label")) for k in res["keywords"]])
check("流水线标注 auto_label 存在", all("auto_label" in k for k in res["keywords"]))
check("流水线标注 memory_label 存在", all("memory_label" in k for k in res["keywords"]))
check("优化被记忆为负面", kws.get("优化", {}).get("memory_label") == "negative")
check("表情/贴纸未进入关键词", not any(is_emoji_or_sticker(w) for w in kws))
check("正常词进入关键词(优化)", "优化" in kws)

print()
print("RESULT:", "ALL PASS" if fail == 0 else f"{fail} FAILED")
sys.exit(1 if fail else 0)
