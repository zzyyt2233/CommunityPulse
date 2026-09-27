"""分词（jieba 可选，缺失则内置最大匹配兜底）与词频 / 新词发现。"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from functools import lru_cache

from ..core.config import DICT_DIR

try:
    import jieba  # type: ignore
except Exception:  # pragma: no cover
    jieba = None  # type: ignore

try:  # 词性标注：用于只保留实词，避免「的/了/很」这类虚词进入统计
    import jieba.posseg as _posseg  # type: ignore
except Exception:  # pragma: no cover
    _posseg = None  # type: ignore

HAS_JIEBA = jieba is not None

_CJK = re.compile(r"[\u4e00-\u9fa5]")
_TOKEN_KEEP = re.compile(r"^[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9+\-_.]{0,15}$")
_PUNCT = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")

# 表情 / 贴纸类词条：不应进入词频统计。
# 覆盖四类：unicode 绘文字、[贴纸码]（微博/B站/贴吧风格，如 [赞][doge][滑稽]）、
# :shortcode:（Slack/Discord 风格）、常见颜文字。
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F\U0000200D\U00002190-\U000021FF\U0001F1E6-\U0001F1FF]"
)
# 全局（文本级）匹配：用于把整段贴纸 / 表情从文本中剔除
_BRACKET_STICKER_GLOBAL = re.compile(r"\[[^\[\]]{1,16}\]")
_SHORTCODE_GLOBAL = re.compile(r":[a-z0-9_+\-]{2,30}:")
_BRACKET_STICKER_RE = re.compile(r"^\[[^\[\]]{1,16}\]$")
_SHORTCODE_RE = re.compile(r"^:[a-z0-9_+\-]{2,30}:$")
_KAOMOJI = {
    "orz", "otz", "t_t", "qaq", "tat", "x_x", ">_<", "@_@", "*_*", "=_=", "-_-",
    "^_^", "^^", "owo", "uwu", "qwq", "tvt", "tt", "qq", "psp", "rmb",
}


def is_emoji_or_sticker(tok: str) -> bool:
    """判断一个 token 是否属于表情 / 贴纸类（不应计入词频统计）。"""
    if not tok:
        return False
    if _BRACKET_STICKER_RE.match(tok):
        return True
    if _SHORTCODE_RE.match(tok):
        return True
    if tok.lower() in _KAOMOJI:
        return True
    # 含任意绘文字字符即视为表情类
    if _EMOJI_RE.search(tok):
        return True
    return False


def strip_emoji(text: str) -> str:
    """去掉文本中的绘文字字符，保留其余内容。"""
    return _EMOJI_RE.sub("", (text or ""))


def clean_emoji_stickers(text: str) -> str:
    """在分词前清掉整段表情 / 贴纸：[:shortcode:]、[贴纸码]、unicode 绘文字。

    之所以在文本层做，是因为 jieba 常把「[滑稽]」拆成「[」和「滑稽」，
    只在 token 层判断会漏掉贴纸名；整段剔除最稳妥。
    """
    if not text:
        return ""
    t = _SHORTCODE_GLOBAL.sub(" ", text)
    t = _BRACKET_STICKER_GLOBAL.sub(" ", t)
    t = _EMOJI_RE.sub(" ", t)
    return t


@lru_cache(maxsize=1)
def _stopwords() -> frozenset[str]:
    words = set()
    f = DICT_DIR / "stopwords.txt"
    if f.exists():
        words |= {w.strip() for w in f.read_text(encoding="utf-8").splitlines() if w.strip()}
    words |= {
        "的", "了", "是", "在", "和", "就", "也", "都", "有", "我", "你", "他", "这", "那",
        "个", "们", "上", "下", "里", "到", "说", "着", "过", "还", "要", "会", "能", "对",
        "吧", "啊", "呢", "吗", "哦", "嗯", "呀", "哈", "啦", "一", "不", "没", "很", "太",
    }
    return frozenset(words)


@lru_cache(maxsize=1)
def _userdict_terms() -> tuple[str, ...]:
    f = DICT_DIR / "userdict.txt"
    if not f.exists():
        return ()
    terms = []
    for line in f.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if parts:
            terms.append(parts[0])
    return tuple(sorted(set(terms), key=len, reverse=True))


def add_user_words(words: list[str]) -> None:
    """界面上补充的自定义词，运行时即时生效。"""
    if not words or jieba is None:
        return
    for w in words:
        w = (w or "").strip()
        if w:
            jieba.add_word(w)


def init_jieba() -> None:
    if jieba is None:
        return
    f = DICT_DIR / "userdict.txt"
    if f.exists():
        try:
            jieba.load_userdict(str(f))
        except Exception:
            pass


init_jieba()


def _fallback_cut(text: str) -> list[str]:
    """无 jieba 时的正向最大匹配 + 单字兜底。"""
    vocab = _userdict_terms()
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        matched = ""
        for w in vocab:
            if len(w) > 8:
                continue
            if text.startswith(w, i):
                matched = w
                break
        if matched:
            out.append(matched)
            i += len(matched)
            continue
        ch = text[i]
        if _CJK.match(ch):
            # 连续英文数字单独成词
            j = i
            while j < n and _CJK.match(text[j]):
                j += 1
            seg = text[i:j]
            if len(seg) <= 2:
                out.append(seg)
            else:
                out.extend([seg[k:k + 2] for k in range(0, len(seg) - 1, 2)]
                           if len(seg) % 2 == 0 else list(seg))
            i = j
        else:
            j = i
            while j < n and not _CJK.match(text[j]) and not text[j].isspace():
                j += 1
            seg = text[i:j].strip()
            if seg:
                out.append(seg)
            i = j if j > i else i + 1
    return out


def cut(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if HAS_JIEBA:
        return [w for w in jieba.cut(text)]
    return _fallback_cut(text)


def tokenize(text: str, extra_stop: set[str] | None = None, min_len: int = 2) -> list[str]:
    """产出可用于统计的词：去停用词、去单字噪声、排除表情/贴纸词条、保留中英文词。"""
    sw = _stopwords() | (extra_stop or set())
    # 先在文本层清掉 [贴纸] / :shortcode: / 绘文字，再分词（避免 jieba 拆出贴纸名）
    cleaned = clean_emoji_stickers(text)
    out = []
    for w in cut(cleaned):
        w = w.strip().lower()
        if not w or w in sw:
            continue
        # 表情 / 贴纸码直接剔除（如 [赞]、[doge]、😀、:smile:、orz）
        if is_emoji_or_sticker(w):
            continue
        # 含绘文字的残留词先剥离表情再判断（如「赞😀」→「赞」）
        if _EMOJI_RE.search(w):
            w = strip_emoji(w).strip().lower()
            if not w or w in sw or is_emoji_or_sticker(w):
                continue
        if len(w) < min_len and not _CJK.match(w):
            continue
        if len(w) == 1 and _CJK.match(w) and w not in ("氪", "肝", "坑", "雷"):
            continue
        if not _TOKEN_KEEP.match(w):
            continue
        if w.isdigit():
            continue
        out.append(w)
    return out


def word_freq(docs: list[list[str]]) -> Counter:
    c: Counter = Counter()
    for d in docs:
        c.update(set(d))  # 文档内去重，避免单条评论刷屏
    return c


def tfidf_keywords(docs: list[list[str]], top_n: int = 60, min_df: int = 2) -> list[tuple[str, float, int]]:
    """按 TF-IDF 排序取关键词，返回 (词, 权重, 出现文档数)。"""
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    n = max(len(docs), 1)
    tf: Counter = Counter()
    for d in docs:
        tf.update(d)
    scores = {}
    for w, f in tf.items():
        if df[w] < min_df:
            continue
        idf = math.log((n + 1) / (df[w] + 0.5)) + 1
        scores[w] = (1 + math.log(f)) * idf
    return [(w, round(s, 3), df[w]) for w, s in
            sorted(scores.items(), key=lambda kv: (-kv[1], -df[kv[0]]))[:top_n]]


def char_ngrams(text: str, n: int = 2) -> list[str]:
    s = _PUNCT.sub("", (text or "").lower())
    if len(s) < n:
        return [s] if s else []
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def discover_new_words(texts: list[str], top_n: int = 30, min_count: int = 3) -> list[dict]:
    """基于凝固度（互信息近似）的简易新词发现，捞出「游戏黑话 / 新梗」。"""
    corpus = "".join(_PUNCT.sub("", clean_emoji_stickers(t or "").lower()) for t in texts)
    if len(corpus) < 200:
        return []
    uni = Counter(corpus)
    total = len(corpus)
    cand: Counter = Counter()
    for n in (2, 3, 4):
        cnt = Counter(char_ngrams(corpus, n))
        for g, c in cnt.items():
            if c >= min_count:
                cand[g] += c
    out = []
    for g, c in cand.items():
        left, right = g[:-1], g[1:]
        pl = uni.get(g[0], 1) / total
        pr = uni.get(g[-1], 1) / total
        pg = c / total
        # 凝固度：实际共现概率与独立假设下的比值
        cohesion = pg / max(pl * pr, 1e-9) if len(g) == 2 else pg / max(
            (uni.get(left, 1) / total) * (uni.get(right, 1) / total), 1e-12)
        if cohesion < 8:
            continue
        if g[0] in _stopwords() or g[-1] in _stopwords():
            continue
        if not _CJK.search(g):
            continue
        # 表情 / 贴纸不应作为「新词 / 黑话」冒出来
        if is_emoji_or_sticker(g) or _EMOJI_RE.search(g):
            continue
        out.append({"word": g, "count": c, "cohesion": round(cohesion, 1)})
    out.sort(key=lambda x: (-x["count"] * math.log1p(x["cohesion"]), -x["count"]))
    # 去掉被更长词完全包含且频次相近的短词
    filtered = []
    for item in out:
        w = item["word"]
        if any(w != o["word"] and w in o["word"] and o["count"] >= item["count"] * 0.8
               for o in out[: top_n * 3]):
            continue
        filtered.append(item)
        if len(filtered) >= top_n:
            break
    return filtered


def phrase_freq(texts: list[str], docs_tokens: list[list[str]] | None = None,
                top_n: int = 30, extra_stop: set[str] | None = None) -> list[dict]:
    """常见短语：小句内相邻实词的搭配（比孤立单词更有信息量）。

    保留 docs_tokens 形参只为兼容旧调用；实际短语由实词序列重算，
    这样既不会跨标点拼接，也不会把虚词混进短语里。
    """
    return [{"phrase": p["phrase"], "count": p["count"], "strength": p["strength"]}
            for p in phrase_candidates(texts, extra_stop=extra_stop, top_n=top_n)]


def build_docs(texts: list[str], extra_stop: set[str] | None = None) -> list[list[str]]:
    return [tokenize(t, extra_stop) for t in texts]


def keyword_examples(texts: list[str], words: list[str], limit: int = 3) -> dict[str, list[str]]:
    """为每个关键词找几条包含它的原句，便于运营直接引用。"""
    want = set(words)
    res: dict[str, list[str]] = defaultdict(list)
    for t in texts:
        if len(res) >= len(want) and all(len(v) >= limit for v in res.values()):
            break
        low = t.lower()
        for w in want:
            if len(res.get(w, [])) >= limit:
                continue
            if w in low:
                res[w].append(t.strip()[:160])
    return dict(res)


# ---------------- 短语优先的词条提取 ----------------
#
# 目标：统计出来的「词条」尽量是完整、连贯、有信息量的词或短语，
#       而不是把一句连贯的话拆成一堆孤立无意义的小词。
#
# 做法：
#   1) 按标点 / 空白切成小句（短语不会跨句拼接）；
#   2) 小句内按词性只保留实词（名词 / 动词 / 形容词 / 英文…），丢掉虚词与单字；
#   3) 相邻实词组成 2~3 词短语，用「搭配强度」筛掉偶然共现；
#   4) 长短语覆盖被它包含的短短语；短语整体优先于单词输出。

# 实词词性白名单（jieba/ictclas 标记）：名词类、动词类、形容词类、英文、习用语、成语、简称
_CONTENT_FLAGS = {
    "n", "nr", "ns", "nt", "nz", "ng",           # 名词 / 人名 / 地名 / 机构 / 专名
    "v", "vn", "vd", "vg",                       # 动词 / 名动词 / 副动词
    "a", "an", "ad", "ag",                       # 形容词 / 名形词 / 副形词
    "eng", "l", "i", "j",                        # 英文 / 习用语 / 成语 / 简称
}

# 小句切分：中文标点与空白都视为停顿
_CLAUSE_SPLIT = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9]+")


def split_clauses(text: str) -> list[str]:
    """把评论按标点 / 空白切成小句（同时清掉表情与贴纸码）。"""
    t = clean_emoji_stickers(text or "")
    return [c for c in _CLAUSE_SPLIT.split(t) if c]


def _clause_words(clause: str, sw: frozenset | set) -> list[str]:
    """小句 → 实词序列（去虚词、单字、数字、表情）。"""
    if _posseg is not None:
        pairs = [(w, f) for w, f in _posseg.cut(clause)]
    elif HAS_JIEBA:
        pairs = [(w, "n") for w in jieba.cut(clause)]
    else:
        pairs = [(w, "n") for w in _fallback_cut(clause)]
    out: list[str] = []
    for w, flag in pairs:
        w = (w or "").strip().lower()
        if not w or w in sw or w.isdigit():
            continue
        if is_emoji_or_sticker(w) or _EMOJI_RE.search(w):
            continue
        if _posseg is not None and flag not in _CONTENT_FLAGS:
            continue        # 只有能拿到词性时才做实词过滤；兜底分词靠长度把关
        if len(w) < 2:                          # 单字不成词条（如「卡」「肝」「坑」）
            continue
        if not _TOKEN_KEEP.match(w):
            continue
        out.append(w)
    return out


def doc_clause_words(texts: list[str], extra_stop: set[str] | None = None
                     ) -> list[list[list[str]]]:
    """每篇文本 → 各小句的实词序列。"""
    sw = _stopwords() | (extra_stop or set())
    return [[_clause_words(c, sw) for c in split_clauses(t)] for t in texts]


def _word_stats(docs_clauses: list[list[list[str]]]) -> tuple[Counter, Counter]:
    """统计单词的出现次数（tf）与文档覆盖数（df）。"""
    w_tf: Counter = Counter()
    w_df: Counter = Counter()
    for clauses in docs_clauses:
        seen_w: set[str] = set()
        for words in clauses:
            for w in words:
                w_tf[w] += 1
                seen_w.add(w)
        w_df.update(seen_w)
    return w_tf, w_df


def _is_meaningful_phrase(g: str, sw) -> bool:
    """短语必须含中文、含实词，且不以虚词 / 停用词起止。"""
    if len(g) < 3 or not _CJK.search(g):
        return False
    if is_emoji_or_sticker(g) or _EMOJI_RE.search(g):
        return False
    if g in sw:
        return False
    if _posseg is not None:
        pairs = [(w, f) for w, f in _posseg.cut(g)]
    elif HAS_JIEBA:
        pairs = [(w, "n") for w in jieba.cut(g)]
    else:
        pairs = [(w, "n") for w in _fallback_cut(g)]
    if not pairs:
        return False
    if len(pairs) < 2:
        return False        # 整段本身就是单个词（如「服务器」），交给单词统计
    if pairs[0][0] in sw or pairs[-1][0] in sw:
        return False        # 「的优化」「了一直」这类半截短语
    if all(w in sw for w, _f in pairs):
        return False
    if _posseg is not None and not any(f in _CONTENT_FLAGS for _w, f in pairs):
        return False        # 全是虚词
    return True


def phrase_candidates(texts: list[str], extra_stop: set[str] | None = None,
                      min_count: int = 2, cohesion: float = 8.0,
                      top_n: int = 30, max_n: int = 5) -> list[dict]:
    """提取高频且连贯的短语（字符级搭配 + 凝固度过滤）。

    在小句内部统计 n-gram，所以短语天然不会跨标点拼接；再用凝固度
    （实际共现频次 vs 独立假设下的期望）筛掉「字恰好挨着」的假搭配。
    """
    sw = _stopwords() | (extra_stop or set())
    clauses = [c for t in texts for c in split_clauses(t)]
    if not clauses:
        return []
    uni: Counter = Counter()
    by_len: dict[int, Counter] = {k: Counter() for k in range(2, max_n + 1)}
    by_len[1] = uni
    for cl in clauses:
        uni.update(cl)
        for k in range(2, max_n + 1):
            if len(cl) >= k:
                by_len[k].update(cl[i:i + k] for i in range(len(cl) - k + 1))
    total = sum(uni.values()) or 1

    def cnt_of(s: str) -> int:
        if len(s) == 1:
            return uni.get(s, 0)
        return by_len.get(len(s), Counter()).get(s, 0)

    cands: list[dict] = []
    for k in range(2, max_n + 1):
        for g, c in by_len[k].items():
            if c < min_count or len(g) < 3:
                continue
            # 凝固度：所有切分点上「实际共现 / 独立假设」的最小值
            coh = min((c * total) / max(cnt_of(g[:i]) * cnt_of(g[i:]), 1)
                      for i in range(1, len(g)))
            if coh < cohesion:
                continue
            if not _is_meaningful_phrase(g, sw):
                continue
            cands.append({"phrase": g, "count": c, "strength": round(coh, 1)})
    # 长短语优先；被更长短语包含且频次相近的短短语丢弃
    cands.sort(key=lambda x: (-len(x["phrase"]), -x["count"]))
    kept: list[dict] = []
    for it in cands:
        if any(it["phrase"] != o["phrase"] and it["phrase"] in o["phrase"]
               and o["count"] >= it["count"] * 0.7 for o in kept):
            continue
        kept.append(it)
    kept.sort(key=lambda x: (-x["count"], -x["strength"]))
    return kept[:top_n]


def extract_key_terms(texts: list[str], top_n: int = 60, min_df: int = 2,
                      extra_stop: set[str] | None = None) -> list[dict]:
    """提取统计词条：完整短语优先，其次是有信息量的实词。

    返回 [{"word", "weight", "docs", "kind"(phrase|word), ...}]，
    短语排在单词之前，避免「一句话被拆成几个孤立小词」。
    """
    docs_clauses = doc_clause_words(texts, extra_stop)
    w_tf, w_df = _word_stats(docs_clauses)
    n = max(len(texts), 1)

    # 1) 短语候选（字符级搭配，天然不跨小句）；再统计其文档覆盖数
    cands = phrase_candidates(texts, extra_stop=extra_stop, top_n=max(top_n * 2, 60))
    ph_set = {p["phrase"] for p in cands}
    ph_df: Counter = Counter()
    for t in texts:
        seen: set[str] = set()
        for cl in split_clauses(t):
            for k in range(2, 6):
                for i in range(len(cl) - k + 1):
                    g = cl[i:i + k]
                    if g in ph_set:
                        seen.add(g)
        ph_df.update(seen)
    kept_p: list[dict] = [
        {"word": p["phrase"], "tf": p["count"], "df": int(ph_df.get(p["phrase"], 0)),
         "kind": "phrase", "strength": p["strength"]}
        for p in cands if int(ph_df.get(p["phrase"], 0)) >= 2
    ]

    # 2) 单词候选
    words: list[dict] = []
    phrase_texts = [p["word"] for p in kept_p]
    for w, cnt in w_tf.items():
        df = int(w_df.get(w, 0))
        if df < min_df:
            continue
        words.append({"word": w, "tf": cnt, "df": df, "kind": "word",
                      "subsumed": any(w in p for p in phrase_texts)})

    def score(t: dict) -> float:
        idf = math.log((n + 1) / (t["df"] + 0.5)) + 1
        s = (1 + math.log(t["tf"])) * idf
        if t["kind"] == "phrase":
            s *= 1.2 + 0.06 * (len(t.get("words") or []) - 2)   # 偏好完整短语
        elif t.get("subsumed"):
            s *= 0.55                                          # 已被短语覆盖的单词降权
        return s

    out = kept_p + words
    for t in out:
        t["weight"] = round(score(t), 3)
        t["docs"] = t.pop("df")
        t.pop("tf", None)
    out = [t for t in out if t["weight"] > 0]
    out.sort(key=lambda x: (x["kind"] != "phrase", -x["weight"], -x["docs"]))
    return out[:top_n]
