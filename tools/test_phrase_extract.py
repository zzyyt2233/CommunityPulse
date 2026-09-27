# -*- coding: utf-8 -*-
"""离线验证：词条提取是否以「完整连贯的短语 / 实词」为主。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from app.analysis.text_utils import (extract_key_terms, phrase_candidates,  # noqa: E402
                                     split_clauses, tokenize)

TEXTS = [
    "优化太差了一直闪退，官方也不修",
    "这游戏优化太差，服务器还炸了",
    "画面精美但是优化拉胯，玩不下去",
    "优化问题最多，希望官方尽快优化服务器",
    "服务器一直炸，优化太差了吧",
    "客服态度很差，退款也不给处理",
    "客服态度很好，处理速度很快",
    "这游戏真不错，画面和剧情都很棒",
    "游戏不错但是服务器太卡了，优化太差",
    "打击感很棒，希望优化服务器和优化太差的问题",
]

print("== 小句切分 ==")
print(split_clauses(TEXTS[0]))
print(split_clauses(TEXTS[2]))

print("\n== 短语候选（搭配强度过滤后） ==")
for p in phrase_candidates(TEXTS, top_n=15):
    print(f"  {p['phrase']:<12} x{p['count']:<3} 凝固度 {p['strength']}")

print("\n== 词条（短语优先） ==")
terms = extract_key_terms(TEXTS, top_n=20)
for t in terms:
    print(f"  [{t['kind']:<6}] {t['word']:<12} w={t['weight']:<8} docs={t['docs']}")

kinds = [t["kind"] for t in terms[:8]]
print("\n前 8 个词条的类型：", kinds)

checks = []


def check(name, ok):
    checks.append((name, ok))
    print(("  PASS " if ok else "  FAIL ") + name)


print("\n== 断言 ==")
check("能提取出短语", any(t["kind"] == "phrase" for t in terms))
check("短语整体排在单词之前",
      kinds == sorted(kinds, key=lambda k: k != "phrase"))
check("出现完整搭配「优化太差」", any(t["word"] == "优化太差" for t in terms))
check("单体词不误判为短语（服务器）",
      not any(t["word"] == "服务器" and t["kind"] == "phrase" for t in terms))
check("短语含完整实词组合（客服态度 类）",
      any(t["word"].startswith("客服") and t["kind"] == "phrase" for t in terms))
check("无单字词条", not any(len(t["word"]) < 2 for t in terms))
check("无虚词词条",
      not any(t["word"] in {"的", "了", "但是", "而且", "希望", "一直", "很快"}
              for t in terms))

# 旧分词路径不受影响
check("tokenize 仍可正常工作", "闪退" in tokenize(TEXTS[0]))

bad = [n for n, ok in checks if not ok]
print("\n" + ("ALL PASS" if not bad else f"FAILED: {bad}"))
sys.exit(1 if bad else 0)
