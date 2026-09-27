/**
 * CommunityPulse 浏览器关键词搜索助手（抖音 / 小红书 / TapTap）
 *
 * 适用：搜索接口也过风控、服务端直连抓不到的平台。
 * 原理：在已登录浏览器里打开搜索页，滚动加载结果，收集前 N 条内容链接，
 *      通过 window.__CP_search_done 回传，再由服务端逐条调浏览器采集器抓评论。
 *
 * 由服务端通过 DevTools 协议注入，自动以 window.__CP_SEARCH_TARGET 控制目标条数。
 */
(async function () {
  'use strict';

  const PLATFORMS = {
    douyin: {
      name: '抖音',
      match: /douyin\.com|iesdouyin\.com/,
      // 内容页链接形如 https://www.douyin.com/video/123456
      pathRe: /https?:\/\/(?:www\.)?douyin\.com\/video\/\d+/,
    },
    xiaohongshu: {
      name: '小红书',
      match: /xiaohongshu\.com|xhslink\.com/,
      // 内容页链接形如 https://www.xiaohongshu.com/explore/abc123 或 /discovery/item/abc123
      pathRe: /https?:\/\/(?:www\.)?xiaohongshu\.com\/(?:explore|discovery\/item)\/[a-z0-9]+/,
    },
    taptap: {
      name: 'TapTap',
      match: /taptap\.(cn|com)/,
      // App 页链接形如 https://www.taptap.cn/app/123456
      pathRe: /https?:\/\/(?:www\.)?taptap\.(?:cn|com)\/app\/\d+/,
    },
  };

  const host = location.host;
  let platform = null;
  for (const [key, cfg] of Object.entries(PLATFORMS)) {
    if (cfg.match.test(host)) { platform = key; break; }
  }
  if (!platform) {
    window.__CP_search_done = JSON.stringify({ ok: false, error: '未识别的平台：' + host +
      '\n本脚本支持抖音 / 小红书 / TapTap。' });
    return;
  }

  const TARGET = Math.max(1, parseInt(window.__CP_SEARCH_TARGET || '5', 10) || 5);

  console.log('%c[CommunityPulse] 浏览器搜索 ' + PLATFORMS[platform].name + '，目标 ' + TARGET + ' 条',
    'color:#2d7ff9;font-weight:bold');

  function collect() {
    const urls = new Set();
    document.querySelectorAll('a[href]').forEach((a) => {
      const h = (a.href || '').split('?')[0].split('#')[0];
      if (PLATFORMS[platform].pathRe.test(h)) urls.add(h);
    });
    return Array.from(urls);
  }

  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  // 搜索结果常是独立滚动容器，逐个滚到底以触发加载
  function scrollAll() {
    window.scrollTo(0, document.body.scrollHeight);
    document.querySelectorAll('[class*="result"], [class*="feed"], [class*="list"], [class*="note"], main, [role="main"]')
      .forEach((el) => {
        try { if (el.scrollHeight > el.clientHeight) el.scrollTop = el.scrollHeight; } catch (e) {}
      });
  }

  const found = [];
  let last = 0;
  let idle = 0;
  const MAX_IDLE = 10;
  for (let i = 0; i < 400 && found.length < TARGET; i++) {
    scrollAll();
    await sleep(800);
    for (const u of collect()) {
      if (!found.includes(u)) found.push(u);
    }
    if (i % 5 === 0) {
      console.log('[CommunityPulse] 已收集 ' + found.length + ' / ' + TARGET + ' 条');
    }
    if (found.length === last) { idle++; } else { idle = 0; last = found.length; }
    if (idle >= MAX_IDLE && found.length > 0) {
      console.log('[CommunityPulse] 连续多次无新增，判断已到搜索结果末尾');
      break;
    }
  }

  if (!found.length) {
    window.__CP_search_done = JSON.stringify({
      ok: false,
      error: '没有收集到任何内容链接。\n可能原因：\n1. 搜索页还没加载出结果（先确认页面有内容）\n2. 未登录或平台改版导致链接结构变化',
    });
    return;
  }

  const out = found.slice(0, TARGET);
  console.log('%c[CommunityPulse] 搜索完成，共收集 ' + out.length + ' 条链接',
    'color:#16a34a;font-weight:bold');
  window.__CP_search_done = JSON.stringify({ ok: true, urls: out });
})();
