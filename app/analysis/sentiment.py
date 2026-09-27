"""情感打分与「吐槽点」主题归类。

思路：词典规则为主（可解释、可离线、零成本），同时预留 LLM 增强接口。
"""
from __future__ import annotations

import re
from typing import Iterable

from .lexicon import (DEFAULT_TOPIC, DEGREE, INTENSIFIERS, IRONY_PHRASES, NEGATION,
                      NEGATIVE, POSITIVE, TOPIC_RULES)
from .text_utils import cut

_EMOJI_POS = re.compile(r"\[(?:赞|喜欢|爱心|星星眼|good|支持|鼓掌|笑)\]|(?:\(\^_\^\)|/≧▽≦/)")
_EMOJI_NEG = re.compile(r"\[(?:怒|泪|吐血|黑线|doge|滑稽|生气|难受|捂脸)\]|(?:orz|OTZ|-_-)")


def score_text(text: str, overrides: dict | None = None) -> tuple[str, float, list[str]]:
    """返回 (情感标签, 得分, 命中的情感词)。

    overrides: 来自情感记忆的覆盖项，结构 {"pos": {word: w}, "neg": {word: w},
        "ignore": {word}}。命中 ignore 的词直接跳过；命中 pos/neg 的词用记忆权重覆盖默认词典。
    """
    if not text:
        return "neutral", 0.0, []
    toks = [t.lower() for t in cut(text)]
    raw = text.lower()
    ov_pos = (overrides or {}).get("pos") or {}
    ov_neg = (overrides or {}).get("neg") or {}
    ov_ignore = (overrides or {}).get("ignore") or set()
    hit: list[str] = []
    total = 0.0
    irony_ctx = any(k in raw for k in ("爆率", "概率", "掉率", "优化", "价格", "福利"))
    for i, t in enumerate(toks):
        # 「爆率感人」里的「感人」是反语，不能按褒义词加分
        if t == "感人" and irony_ctx:
            continue
        # 用户记忆：中立词直接忽略，不计入情感
        if t in ov_ignore:
            continue
        # 默认词典优先被记忆覆盖
        if t in ov_pos:
            base = ov_pos[t]
        elif t in ov_neg:
            base = ov_neg[t]
        else:
            base = POSITIVE.get(t) or NEGATIVE.get(t)
        if base is None:
            continue
        weight = 1.0
        # 程度副词看前两个词；否定词只在紧邻时生效，
        # 否则「还是没出货，吃相难看」里的「没」会把「吃相」反转成正分。
        for j in (i - 2, i - 1):
            if j < 0:
                continue
            prev = toks[j]
            if prev in DEGREE:
                weight *= DEGREE[prev]
        if i - 1 >= 0 and toks[i - 1] in NEGATION:
            weight *= -0.85
        val = base * weight
        total += val
        hit.append(t)
    # 感叹 / 疑问语气放大
    for pat, add in INTENSIFIERS:
        if pat in raw:
            total += (add if total >= 0 else -add)
            break
    if _EMOJI_POS.search(raw):
        total += 0.6
    if _EMOJI_NEG.search(raw):
        total -= 0.6
    # 反讽搭配优先：「爆率感人」不是夸，是骂
    for phrase, val in IRONY_PHRASES.items():
        if val and phrase in raw:
            total += val
    # 长度归一：长评论情绪词更多，避免评分虚高
    norm = total / (1 + 0.02 * max(len(text) - 40, 0))
    if norm >= 0.6:
        label = "positive"
    elif norm <= -0.6:
        label = "negative"
    else:
        label = "neutral"
    return label, round(norm, 3), hit


def classify_topics(text: str, extra_rules: dict[str, list[str]] | None = None) -> list[str]:
    """把一条评论归到一个或多个吐槽点主题。"""
    t = (text or "").lower()
    if not t:
        return []
    rules = dict(TOPIC_RULES)
    if extra_rules:
        for k, v in extra_rules.items():
            rules.setdefault(k, [])
            rules[k] = list(rules[k]) + [w.lower() for w in v]
    out = []
    for topic, kws in rules.items():
        for kw in kws:
            if kw.lower() in t:
                out.append(topic)
                break
    return out or [DEFAULT_TOPIC]


def batch_sentiment(texts: Iterable[str]) -> list[dict]:
    res = []
    for t in texts:
        label, score, hit = score_text(t)
        res.append({"label": label, "score": score, "hits": hit})
    return res


# ------------- 可选：大模型增强（在设置里填 OpenAI 兼容接口即可启用） -------------

LLM_PROMPT = """你是资深游戏社区运营分析师。判断以下玩家评论的情感倾向与主要吐槽点。
只输出 JSON，不要解释。格式：
{"sentiment":"positive|neutral|negative","score":-3到3的浮点数,"topics":["主题1"],"reason":"一句话"}

评论：
"""


def llm_analyze(texts: list[str], endpoint: str, api_key: str, model: str = "gpt-4o-mini",
                batch: int = 10, timeout: float = 30.0) -> list[dict]:
    """调用 OpenAI 兼容的 /chat/completions 接口做情感+主题判定。失败时抛异常由上层降级。"""
    import json as _json
    import urllib.request

    out: list[dict] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "你是严谨的游戏舆情分析助手，输出必须是纯 JSON。"},
                {"role": "user", "content": "\n".join(f"{n+1}. {t[:300]}" for n, t in enumerate(chunk))},
            ],
        }
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions",
            data=_json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        try:
            parsed = _json.loads(content)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            parsed = [parsed]
        for j, item in enumerate(parsed[:len(chunk)]):
            out.append({
                "label": str(item.get("sentiment", "neutral")).lower(),
                "score": float(item.get("score", 0) or 0),
                "topics": item.get("topics") or [],
                "reason": item.get("reason", ""),
            })
        while len(out) < min(i + batch, len(texts)):
            out.append({"label": "neutral", "score": 0.0, "topics": [], "reason": ""})
    return out[:len(texts)]
