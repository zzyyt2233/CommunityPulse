"""discovery 搜索适配器离线单测：不依赖网络，用假响应验证解析与注册表完整性。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collectors import discovery as D


def mock_get_json(payload):
    D.get_json = lambda *a, **k: payload


def mock_get_text(html):
    D.get_text = lambda *a, **k: html


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        globals()["_fail"] = True


# 1) 注册表完整性
print("== PLATFORMS 完整性 ==")
for key, cfg in D.PLATFORMS.items():
    if cfg.get("browser"):
        check(f"{key}: browser 平台 fn 为 None", cfg.get("fn") is None)
    else:
        check(f"{key}: 非 browser 平台 fn 可调用", callable(cfg.get("fn")))
    if cfg.get("rank_key"):
        check(f"{key}: rank_key 是合法字段", cfg["rank_key"] in
              ("play", "like"))

# 2) 微博解析
print("\n== 微博 ==")
mock_get_json({"data": {"cards": [
    {"mblog": {"id": "111", "mid": "111", "text": "<em>原神</em> 卡顿反馈",
               "user": {"screen_name": "用户A"},
               "comments_count": 123, "attitudes_count": 456, "reposts_count": 7}},
    {"mblog": {"id": "222", "mid": "222", "text": "", "user": {}}},  # 空文本应跳过
]}})
items = D.search_weibo("原神 卡顿", limit=10, settings={})
check("返回 1 条（空文本被过滤）", len(items) == 1)
check("url 为 m.weibo.cn/detail/111", items and items[0]["url"] == "https://m.weibo.cn/detail/111")
check("标题已去 <em>", items and items[0]["title"] == "原神 卡顿反馈")
check("like=attitudes=456", items and items[0]["like"] == 456)

# 3) 贴吧解析
print("\n== 贴吧 ==")
mock_get_text('<a href="/p/1234567890" title="帖子一">帖子一</a>'
              '<a href="/p/1234567890" title="重复">重复</a>'
              '<a href="/p/2222222222" title="帖子二">帖子二</a>')
items = D.search_tieba("测试", limit=10, settings={})
check("去重后 2 条", len(items) == 2)
check("url 正确", items and items[0]["url"] == "https://tieba.baidu.com/p/1234567890")

# 4) NGA 解析
print("\n== NGA ==")
mock_get_text('<a href="read.php?tid=999&page=1">NGA帖子</a>'
              '<a href="https://bbs.nga.cn/read.php?tid=888">另一帖</a>')
items = D.search_nga("测试", limit=10, settings={})
check("返回 2 条", len(items) == 2)
check("url 含 tid=999", any("tid=999" in it["url"] for it in items))

# 5) TapTap 解析
print("\n== TapTap ==")
mock_get_json({"data": {"list": [
    {"id": 12345, "title": "游戏A", "votes": 98, "comment_count": 50},
    {"id": 67890, "title": "游戏B"},
]}})
items = D.search_taptap("游戏", limit=10, settings={})
check("返回 2 条", len(items) == 2)
check("url 为 taptap.cn/app/12345", items and items[0]["url"] == "https://www.taptap.cn/app/12345")
check("like=votes=98", items and items[0]["like"] == 98)

# 6) search() 分发
print("\n== search() 分发 ==")
check("browser 平台返回空", D.search("douyin", "x") == [])
check("未知平台返回空", D.search("nope", "x") == [])
mock_get_json({"data": {"cards": [{"mblog": {"id": "5", "mid": "5", "text": "t", "user": {}}}]}})
check("search('weibo') 走 search_weibo", len(D.search("weibo", "k", settings={})) == 1)

print("\n结果：", "全部通过" if not globals().get("_fail") else "存在失败")
sys.exit(1 if globals().get("_fail") else 0)
