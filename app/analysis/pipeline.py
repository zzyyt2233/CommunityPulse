"""分析流水线：相似句聚类 + 舆情整合（词频/情感/主题/趋势/重点原句）。"""
from __future__ import annotations

import math
import re
import time
from collections import Counter, defaultdict

from . import comment_memory as cm
from . import sentiment_memory as sm
from .lexicon import CONCEPT_INDEX, NEGATIVE, POSITIVE
from .sentiment import classify_topics, score_text
from .text_utils import (char_ngrams, discover_new_words, extract_key_terms,
                         phrase_freq, tokenize)


# ---------------- 相似句聚类 ----------------

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").lower())


def _vector(text: str) -> dict[str, float]:
    grams = char_ngrams(text, 2)
    if not grams:
        return {}
    c = Counter(grams)
    return {g: 1 + math.log(n) for g, n in c.items()}


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for g, w in a.items():
        if g in b:
            dot += w * b[g]
    if not dot:
        return 0.0
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    return dot / (na * nb + 1e-9)


def _token_vector(tokens: list[str]) -> dict[str, float]:
    c = Counter(tokens)
    return {f"t:{w}": 1 + math.log(n) for w, n in c.items()}


def _idf_weight(vecs: list[dict[str, float]]) -> None:
    df: Counter = Counter()
    for v in vecs:
        df.update(v.keys())
    n = len(vecs)
    for v in vecs:
        for g in list(v):
            v[g] *= (math.log((n + 1) / (df[g] + 1)) + 1)


def similarity(i: int, j: int, vecs_bi: list[dict], vecs_tok: list[dict]) -> float:
    """字符 bigram 相似度 + 实词语义相似度的混合，兼顾「字面不同但诉求相同」。"""
    a = _cos(vecs_bi[i], vecs_bi[j])
    b = _cos(vecs_tok[i], vecs_tok[j])
    return 0.5 * a + 0.5 * b


def concept_set(text: str, tokens: list[str]) -> set[str]:
    """把评论里出现的说法映射成诉求概念集合。"""
    low = (text or "").lower()
    out: set[str] = set()
    for w in tokens or []:
        c = CONCEPT_INDEX.get(w.lower())
        if c:
            out.add(c)
    for surface, c in CONCEPT_INDEX.items():
        if surface in low:
            out.add(c)
    return out


def cluster_texts(texts: list[str], docs_tokens: list[list[str]] | None = None,
                  weights: list[float] | None = None, sentiments: list[str] | None = None,
                  threshold: float = 0.45, max_clusters: int = 80) -> tuple[list[list[int]], list[int]]:
    """贪心 leader 聚类，返回 (分组, 每组的代表句下标)。

    相似度 = 0.6 x 字面相似（字符 bigram + 实词）+ 0.4 x 诉求概念重合 x 情感一致性。
    这样「爆率感人」和「出货太难」这类用词不同但诉求相同的反馈能归到一起，
    而「优化差」与「优化变好了」不会被强行合并。
    """
    n = len(texts)
    if n == 0:
        return [], []
    vecs_bi = [_vector(_norm(t)) for t in texts]
    vecs_tok = [_token_vector(docs_tokens[i] if docs_tokens and i < len(docs_tokens) else [])
                for i in range(n)]
    _idf_weight(vecs_bi)
    _idf_weight(vecs_tok)
    docs_tokens = docs_tokens or [[] for _ in range(n)]
    concepts = [concept_set(texts[i], docs_tokens[i]) for i in range(n)]
    sents = sentiments or ["neutral"] * n

    def sim(i: int, j: int) -> float:
        literal = similarity(i, j, vecs_bi, vecs_tok)
        ci, cj = concepts[i], concepts[j]
        # 用重叠系数：诉求是子集关系时（如「外挂」vs「外挂+客服」）视为同一诉求
        cjac = (len(ci & cj) / min(len(ci), len(cj))) if (ci and cj) else 0.0
        if sents[i] == sents[j]:
            factor = 1.0
        elif "neutral" in (sents[i], sents[j]):
            factor = 0.5
        else:  # 一正一负
            factor = 0.3
        return 0.55 * literal + 0.45 * cjac * factor

    order = sorted(range(n), key=lambda i: -(weights[i] if weights else 0.0))
    leaders: list[int] = []
    groups: list[list[int]] = []
    labels = [-1] * n
    for i in order:
        best, best_sim = -1, 0.0
        for li, lidx in enumerate(leaders):
            s = sim(i, lidx)
            if s > best_sim:
                best_sim, best = s, li
        if best_sim >= threshold and best >= 0:
            labels[i] = best
            groups[best].append(i)
        elif len(leaders) < max_clusters:
            leaders.append(i)
            groups.append([i])
            labels[i] = len(groups) - 1
        else:
            best = max(range(len(leaders)), key=lambda li: sim(i, leaders[li]))
            labels[i] = best
            groups[best].append(i)

    # 代表句：与同簇成员平均相似度最高的一条
    centers: list[int] = []
    for g in groups:
        if len(g) == 1:
            centers.append(g[0])
            continue
        sample = g[:40]
        best_i, best_s = g[0], -1.0
        for i in sample:
            s = sum(sim(i, j) for j in sample) / len(sample)
            if s > best_s:
                best_s, best_i = s, i
        centers.append(best_i)
    return groups, centers


def _cluster_keywords(idxs: list[int], docs: list[list[str]], top_k: int = 5) -> list[str]:
    c: Counter = Counter()
    for i in idxs:
        c.update(set(docs[i]))
    return [w for w, _ in c.most_common(top_k)]


# ---------------- 主流程 ----------------

def analyze(comments: list[dict], params: dict | None = None) -> dict:
    p = dict(params or {})
    top_n = int(p.get("top_n", 60))
    threshold = float(p.get("cluster_threshold", 0.45))
    extra_stop = set(w.strip() for w in (p.get("extra_stopwords") or "").split() if w.strip())
    extra_topics = p.get("custom_topics") or {}
    min_len = int(p.get("min_len", 2))
    max_clusters = int(p.get("max_clusters", 80))

    # 1) 清洗与去重
    bucket: dict[str, dict] = {}
    for c in comments:
        txt = (c.get("content") or "").strip()
        if len(txt) < min_len:
            continue
        key = _norm(txt)[:120]
        if key in bucket:
            bucket[key]["dup"] += 1
            bucket[key]["like_count"] = max(bucket[key]["like_count"], int(c.get("like_count") or 0))
            continue
        d = dict(c)
        d["dup"] = 1
        bucket[key] = d
    rows = list(bucket.values())

    # 只分析「热度前 N 条」：按点赞数降序截取（平台没有点赞数据时自动忽略）
    notes: list[str] = []
    top_liked = int(p.get("top_liked_n") or 0)
    pool_size = len(rows)
    liked_rows = sum(1 for r in rows if int(r.get("like_count") or 0) > 0)
    likes_available = liked_rows >= max(3, pool_size * 0.1)
    if top_liked > 0:
        if not likes_available:
            notes.append("该来源的评论没有点赞数据，已忽略「热度前 N 条」设置，按全部评论分析")
        elif pool_size > top_liked:
            rows.sort(key=lambda r: (-int(r.get("like_count") or 0), -len(r["content"])))
            rows = rows[:top_liked]

    texts = [r["content"] for r in rows]

    if not texts:
        return _empty_result("没有可分析的评论内容")

    # 2) 分词与词条（短语优先：输出完整连贯的搭配，而不是被拆碎的孤立小词）
    docs = [tokenize(t, extra_stop) for t in texts]
    keywords = extract_key_terms(texts, top_n=top_n, min_df=2, extra_stop=extra_stop)
    new_words = discover_new_words(texts, top_n=30)
    phrases = phrase_freq(texts, docs, top_n=30)

    # 载入用户情感记忆（词条纠错 + 语句级记忆），并转成打分覆盖项
    memory = sm.load_memory()
    overrides = sm.overrides_to_lexicon(memory)
    for k in keywords:
        w = (k["word"] or "").lower()
        k["auto_label"] = sm.auto_label_of(w, POSITIVE, NEGATIVE)  # 默认词典基线
        k["memory_label"] = sm.memory_label_of(w, memory)          # 用户记忆标签

    # 语句级记忆：同一条评论若被人工标过，跨来源 / 跨任务都直接沿用人工标签
    cmemory = cm.load_memory()
    for r in rows:
        r["key"] = cm.norm_key(r["content"])
        r["mem_label"] = cm.label_of(cmemory, r["key"])

    # 3) 情感（记忆优先级：语句记忆 > 词条记忆 > 默认词典）
    sent = [score_text(t, overrides) for t in texts]
    for r, s in zip(rows, sent):
        label, score, hits = s
        r["sent_source"] = "memory" if r.get("mem_label") else "auto"
        if r.get("mem_label"):
            # 人工标注为准：标签覆盖，得分同步到同号，避免「标签正面、得分却是负」的自相矛盾
            label = r["mem_label"]
            if label == "neutral":
                score = 0.0
            elif label == "positive":
                score = abs(score)
            else:
                score = -abs(score)
        r["sentiment"], r["sent_score"], r["sent_hits"] = label, score, hits

    # 4) 主题归类
    topic_stat: dict[str, dict] = {}
    for r in rows:
        topics = classify_topics(r["content"], extra_topics)
        r["topics"] = topics
        for tp in topics:
            st = topic_stat.setdefault(tp, {"topic": tp, "count": 0, "positive": 0,
                                            "neutral": 0, "negative": 0, "likes": 0,
                                            "samples": [], "keywords": []})
            st["count"] += 1
            st["likes"] += int(r.get("like_count") or 0)
            st[r["sentiment"]] += 1
            if len(st["samples"]) < 8 and int(r.get("like_count") or 0) >= 0:
                st["samples"].append({
                    "content": r["content"][:200],
                    "like_count": int(r.get("like_count") or 0),
                    "sentiment": r["sentiment"],
                    "user": r.get("user_name", ""),
                    "time": r.get("published_at"),
                    "key": r.get("key", ""),
                    "mem_label": r.get("mem_label"),
                })
    for tp, st in topic_stat.items():
        idxs = [i for i, r in enumerate(rows) if tp in r.get("topics", [])]
        st["keywords"] = _cluster_keywords(idxs, docs, 6)
        st["samples"] = sorted(st["samples"], key=lambda x: -x["like_count"])[:5]
        st["neg_rate"] = round(st["negative"] / max(st["count"], 1), 3)
    topics_sorted = sorted(topic_stat.values(), key=lambda x: -x["count"])

    # 5) 相似句聚类
    weights = [math.log1p(int(r.get("like_count") or 0)) * 2 + math.log1p(len(r["content"]))
               for r in rows]
    sentiments = [r["sentiment"] for r in rows]
    groups, centers = cluster_texts(texts, docs, weights, sentiments=sentiments,
                                    threshold=threshold, max_clusters=max_clusters)
    clusters = []
    for gi, g in enumerate(groups):
        if len(g) < 1:
            continue
        center = centers[gi]
        members = sorted(g, key=lambda i: -int(rows[i].get("like_count") or 0))
        sent_dist = Counter(rows[i]["sentiment"] for i in g)
        clusters.append({
            "size": len(g),
            "representative": texts[center][:220],
            "rep_meta": {
                "user": rows[center].get("user_name", ""),
                "like_count": int(rows[center].get("like_count") or 0),
                "sentiment": rows[center]["sentiment"],
                "time": rows[center].get("published_at"),
                "key": rows[center].get("key", ""),
                "mem_label": rows[center].get("mem_label"),
            },
            "keywords": _cluster_keywords(g, docs, 6),
            "samples": [{
                "content": texts[i][:200],
                "like_count": int(rows[i].get("like_count") or 0),
                "sentiment": rows[i]["sentiment"],
                "user": rows[i].get("user_name", ""),
                "time": rows[i].get("published_at"),
                "key": rows[i].get("key", ""),
                "mem_label": rows[i].get("mem_label"),
            } for i in members if i != center][:5],
            "sentiment": {k: sent_dist.get(k, 0) for k in ("positive", "neutral", "negative")},
            "total_likes": sum(int(rows[i].get("like_count") or 0) for i in g),
            "topics": [t for t, _ in Counter(
                tp for i in g for tp in rows[i].get("topics", [])).most_common(3)],
            "concepts": [c for c, _ in Counter(
                cp for i in g for cp in concept_set(texts[i], docs[i])).most_common(3)],
        })
    clusters.sort(key=lambda c: (-c["size"], -c["total_likes"]))

    # 6) 时间趋势
    trend = _build_trend(rows)

    # 7) 重点舆情原句
    highlights = _build_highlights(rows, clusters)

    # 8) 汇总
    dist = Counter(r["sentiment"] for r in rows)
    total = len(rows)
    times = [r.get("published_at") for r in rows if r.get("published_at")]
    return {
        "generated_at": time.time(),
        "params": p,
        "stats": {
            "total": total,
            "raw_total": len(comments),
            "duplicates_merged": len(comments) - total,
            "avg_length": round(sum(len(t) for t in texts) / total, 1),
            "positive": dist.get("positive", 0),
            "neutral": dist.get("neutral", 0),
            "negative": dist.get("negative", 0),
            "positive_rate": round(dist.get("positive", 0) / total, 3),
            "negative_rate": round(dist.get("negative", 0) / total, 3),
            "time_from": min(times) if times else None,
            "time_to": max(times) if times else None,
            "has_time": bool(times),
            "pool_size": pool_size,
            "top_liked_n": top_liked if (top_liked > 0 and likes_available) else 0,
            "likes_available": likes_available,
            "memory_marked": sum(1 for r in rows if r.get("mem_label")),
        },
        "notes": notes,
        "keywords": keywords,
        "new_words": new_words,
        "phrases": phrases,
        "clusters": clusters[:40],
        "topics": topics_sorted,
        "trend": trend,
        "highlights": highlights,
        # 只列真正判为该极性的句子（否则「最正面原声」里会混进负分评论）
        "sentiment_examples": {
            "negative": [{"content": r["content"][:200], "like_count": int(r.get("like_count") or 0),
                          "score": r["sent_score"], "user": r.get("user_name", ""),
                          "key": r.get("key", ""), "mem_label": r.get("mem_label")}
                         for r in sorted((r for r in rows if r["sentiment"] == "negative"),
                                         key=lambda x: (x["sent_score"], -int(x.get("like_count") or 0)))[:15]],
            "positive": [{"content": r["content"][:200], "like_count": int(r.get("like_count") or 0),
                          "score": r["sent_score"], "user": r.get("user_name", ""),
                          "key": r.get("key", ""), "mem_label": r.get("mem_label")}
                         for r in sorted((r for r in rows if r["sentiment"] == "positive"),
                                         key=lambda x: (-x["sent_score"], -int(x.get("like_count") or 0)))[:15]],
        },
    }


def _build_trend(rows: list[dict]) -> dict:
    times = [(r.get("published_at"), r) for r in rows if r.get("published_at")]
    if len(times) < 5:
        return {"available": False, "points": [], "reason": "评论缺少可用时间戳（少于 5 条）"}
    buckets: dict[str, dict] = defaultdict(lambda: {"date": "", "count": 0, "positive": 0,
                                                    "neutral": 0, "negative": 0, "likes": 0})
    for ts, r in times:
        day = time.strftime("%Y-%m-%d", time.localtime(float(ts)))
        b = buckets[day]
        b["date"] = day
        b["count"] += 1
        b[r.get("sentiment", "neutral")] += 1
        b["likes"] += int(r.get("like_count") or 0)
    pts = sorted(buckets.values(), key=lambda x: x["date"])
    for b in pts:
        b["neg_rate"] = round(b["negative"] / max(b["count"], 1), 3)
    return {"available": True, "points": pts, "days": len(pts)}


def _build_highlights(rows: list[dict], clusters: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(content: str, like: int, reason: str, sentiment: str, user: str, ts,
            key: str = "", mem_label=None):
        sig = _norm(content)[:100]
        if sig in seen:
            return
        seen.add(sig)
        out.append({"content": content[:300], "like_count": like, "reason": reason,
                    "sentiment": sentiment, "user": user, "time": ts,
                    "key": key, "mem_label": mem_label})

    for r in sorted(rows, key=lambda x: -int(x.get("like_count") or 0))[:10]:
        if int(r.get("like_count") or 0) > 0:
            add(r["content"], int(r.get("like_count") or 0), "高赞原声",
                r["sentiment"], r.get("user_name", ""), r.get("published_at"),
                r.get("key", ""), r.get("mem_label"))
    for c in clusters[:12]:
        if c["size"] >= 2:
            add(c["representative"], c["rep_meta"]["like_count"],
                f"高频诉求 · 相似 {c['size']} 条", c["rep_meta"]["sentiment"],
                c["rep_meta"]["user"], c["rep_meta"]["time"],
                c["rep_meta"].get("key", ""), c["rep_meta"].get("mem_label"))
    for r in sorted(rows, key=lambda x: (x["sent_score"], -int(x.get("like_count") or 0)))[:10]:
        if r["sent_score"] <= -2:
            add(r["content"], int(r.get("like_count") or 0), "强烈负面 · 需优先处理",
                r["sentiment"], r.get("user_name", ""), r.get("published_at"),
                r.get("key", ""), r.get("mem_label"))
    out.sort(key=lambda x: (-x["like_count"], x["sentiment"] == "negative"))
    return out[:30]


def _empty_result(msg: str) -> dict:
    return {"generated_at": time.time(), "error": msg, "stats": {"total": 0},
            "keywords": [], "new_words": [], "phrases": [], "clusters": [],
            "topics": [], "trend": {"available": False, "points": []},
            "highlights": [], "sentiment_examples": {"negative": [], "positive": []}}
