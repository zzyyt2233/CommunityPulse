/**
 * CommunityPulse 浏览器评论采集助手
 *
 * 适用：抖音 / 小红书 / TapTap 这类有签名风控、服务端直连抓不到的平台。
 * 原理：不改任何请求、不算签名，只是把你浏览器「本来就会收到」的评论数据拦下来汇总导成 JSON。
 *      用的是你自己的登录态，属于正常浏览行为，不构成绕过风控。
 *
 * 两种用法：
 *  A. 手动（控制台粘贴）：按 F12 → Console → 粘贴本文件 → 回车 → 输入目标条数 → 自动滚动抓完。
 *  B. 自动（工具一键调用）：工具服务端通过 DevTools 协议注入本脚本，自动以 window.__CP_AUTO 模式运行，
 *     不弹窗、直接用 window.__CP_BASE 把结果回传，并通过 window.__CP_done 通知工具完成。
 *
 * 注意：脚本会滚动页面触发加载，跑的过程中不要关页面。
 */
(async function () {
  'use strict';

  // 自动模式：由工具服务端注入 window.__CP_AUTO / __CP_TARGET / __CP_BASE 控制
  const AUTO = (typeof window.__CP_AUTO !== 'undefined' && window.__CP_AUTO === true);

  // 追加模式：服务端下发已有 task_id（关键词采集时多个视频/笔记合并进同一个任务），
  // 为空则为本次采集新建任务（单链接采集、手动粘贴）。
  const TASK_ID = (AUTO && window.__CP_TASK_ID)
    ? (parseInt(window.__CP_TASK_ID, 10) || 0) : 0;

  const PLATFORMS = {
    douyin: { name: '抖音', match: /douyin\.com|iesdouyin\.com/ },
    xiaohongshu: { name: '小红书', match: /xiaohongshu\.com|xhslink\.com/ },
    taptap: { name: 'TapTap', match: /taptap\.(cn|com)/ },
  };

  const host = location.host;
  let platform = null;
  for (const [key, cfg] of Object.entries(PLATFORMS)) {
    if (cfg.match.test(host)) { platform = key; break; }
  }
  if (!platform) {
    const msg = '未识别的平台：' + host + '\n本脚本支持 抖音 / 小红书 / TapTap。\nB站请用工具里的直接抓取（已内置，无需此脚本）。';
    if (AUTO) { window.__CP_done = JSON.stringify({ ok: false, error: msg }); return; }
    alert(msg);
    return;
  }

  // 目标条数：自动模式从 __CP_TARGET 取，手动模式弹窗询问
  let TARGET = 500;
  if (AUTO) {
    TARGET = Math.max(1, parseInt(window.__CP_TARGET || '500', 10) || 500);
  } else {
    const input = prompt('【' + PLATFORMS[platform].name + '】要采集多少条评论？', '500');
    TARGET = Math.max(1, parseInt(input || '500', 10) || 500);
  }

  // 自动模式下用 console 提示代替弹窗，并把进度/结果写到 window.__CP_* 供工具轮询
  function note(msg) {
    if (AUTO) { console.warn('[CommunityPulse] ' + msg); }
    else { alert(msg); }
  }
  function fail(msg) {
    if (AUTO) { window.__CP_done = JSON.stringify({ ok: false, error: msg }); }
    else { alert(msg); }
  }

  console.log('%c[CommunityPulse] 开始采集 ' + PLATFORMS[platform].name + '，目标 ' + TARGET + ' 条' +
    (AUTO ? '（自动模式）' : ''), 'color:#2d7ff9;font-weight:bold');

  // ---------- 1. 拦截 XHR / fetch 响应 ----------
  const seen = new Set();
  const found = [];

  function pickComment(o) {
    if (!o || typeof o !== 'object') return null;
    const content = typeof o.text === 'string' ? o.text
      : typeof o.content === 'string' ? o.content : null;
    if (!content || !content.trim()) return null;
    const user = o.user || o.user_info || o.author || null;
    const nick = (user && (user.nickname || user.nick_name || user.name)) || o.nickname || o.user_name || '';
    const uid = o.cid || o.id || o.comment_id || '';
    if (!uid) return null;
    const like = (typeof o.votes === 'object' && o.votes)
      ? (o.votes.up ?? o.votes.like ?? 0)
      : (o.digg_count ?? o.like_count ?? o.digg ?? o.likes ?? 0);
    return {
      comment_id: String(uid),
      user_name: String(nick || ''),
      content: String(content).replace(/\s+/g, ' ').trim(),
      like_count: Number(like) || 0,
      reply_count: Number(o.reply_comment_total ?? o.sub_comment_count ?? o.reply_count ?? 0) || 0,
      published_at: Number(o.create_time ?? o.time ?? 0) || null,
    };
  }

  function harvest(node, depth) {
    if (depth > 8 || !node) return;
    if (Array.isArray(node)) {
      for (const item of node) {
        const c = pickComment(item);
        if (c && !seen.has(c.comment_id)) { seen.add(c.comment_id); found.push(c); }
        harvest(item, depth + 1);
      }
      return;
    }
    if (typeof node !== 'object') return;
    for (const key of Object.keys(node)) {
      const val = node[key];
      if (val && typeof val === 'object') harvest(val, depth + 1);
    }
    // 顶层也可能直接是一条评论
    const c = pickComment(node);
    if (c && !seen.has(c.comment_id)) { seen.add(c.comment_id); found.push(c); }
  }

  const origOpen = XMLHttpRequest.prototype.open;
  const origSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__cpUrl = String(url || '');
    return origOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    this.addEventListener('load', function () {
      try {
        if (!/comment|reply|review/i.test(this.__cpUrl || '')) return;
        harvest(JSON.parse(this.responseText), 0);
      } catch (e) { /* 非 JSON 响应忽略 */ }
    });
    return origSend.apply(this, arguments);
  };

  const origFetch = window.fetch;
  window.fetch = function () {
    const p = origFetch.apply(this, arguments);
    const url = String((arguments[0] && arguments[0].url) || arguments[0] || '');
    return p.then((res) => {
      if (res.ok && /comment|reply|review/i.test(url)) {
        res.clone().json().then((j) => harvest(j, 0)).catch(() => {});
      }
      return res;
    });
  };

  // ---------- 2. 自动滚动加载 ----------
  function sleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

  function scrollTick() {
    window.scrollTo(0, document.body.scrollHeight);
    // 抖音/小红书的评论区常常是内部滚动容器，两个都滚一遍
    const inner = document.querySelector(
      '[data-e2e="comment-list"], .comments-container, .comment-scroll, [class*="comment"]'
    );
    if (inner && inner.scrollHeight > inner.clientHeight) {
      inner.scrollTop = inner.scrollHeight;
    }
    // 尝试点「展开更多回复」
    const more = document.querySelector(
      '[data-e2e="comment-expand"], .show-more, [class*="expand-more"]'
    );
    if (more && typeof more.click === 'function') { try { more.click(); } catch (e) {} }
  }

  let idle = 0;
  const MAX_IDLE = 12;   // 连续 12 次没新增就认为到底了
  let last = 0;
  if (AUTO) window.__CP_status = { running: true, count: 0 };
  for (let i = 0; i < 400 && found.length < TARGET; i++) {
    scrollTick();
    await sleep(900);
    if (found.length === last) { idle++; } else { idle = 0; last = found.length; }
    if (AUTO) window.__CP_status = { running: true, count: found.length };
    if (i % 5 === 0) {
      console.log('[CommunityPulse] 已采集 ' + found.length + ' / ' + TARGET + ' 条');
    }
    if (idle >= MAX_IDLE) {
      console.log('[CommunityPulse] 连续多次无新增，判断已到评论区末尾');
      break;
    }
  }

  window.scrollTo(0, 0);

  // ---------- 3. 导出 ----------
  window.fetch = origFetch;
  XMLHttpRequest.prototype.open = origOpen;
  XMLHttpRequest.prototype.send = origSend;

  if (!found.length) {
    fail('没有采集到评论。\n可能原因：\n1. 页面还没加载到评论区（先手动往下滚一点再运行）\n2. 未登录或平台改版导致接口路径变了\n建议确认页面能看到评论后重试。');
    return;
  }

  const payload = {
    platform: platform,
    source_url: location.href,
    source_title: document.title.replace(/\s*[-|—].*$/, '').trim(),
    exported_at: Math.floor(Date.now() / 1000),
    count: found.length,
    comments: found.slice(0, TARGET),
  };
  const text = JSON.stringify(payload, null, 2);

  // 自动模式优先用工具下发的回传地址；手动模式自己探测本地服务端口
  const base = (window.__CP_BASE && String(window.__CP_BASE).indexOf('http') === 0)
    ? window.__CP_BASE
    : (await detectService());

  if (base) {
    try {
      const res = await fetch(base + '/api/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          text: text, fmt: 'json', name: payload.source_title,
          // 追加模式：并入服务端指定的任务；否则新建任务
          task_id: TASK_ID || undefined,
        }),
      });
      const j = await res.json();
      if (j.ok) {
        console.log('%c[CommunityPulse] 已回传并入库：任务 #' + j.task_id + '，本次 ' +
          payload.count + ' 条' + (j.appended ? '（累计 ' + (j.total || 0) + ' 条）' : ''),
          'color:#16a34a;font-weight:bold');
        if (AUTO) {
          window.__CP_done = JSON.stringify({ ok: true, count: payload.count,
                                              task_id: j.task_id,
                                              total: j.total || payload.count });
        } else {
          console.log('回到 CommunityPulse 页面刷新任务列表即可看到，直接点「分析」。');
          alert('采集完成：' + payload.count + ' 条\n已自动入库为任务 #' + j.task_id +
            '\n回到 CommunityPulse 刷新任务列表即可分析。');
        }
        return;
      }
      console.warn('[CommunityPulse] 回传失败：' + (j.error || '未知错误') + '，改为下载文件');
    } catch (e) {
      console.warn('[CommunityPulse] 回传失败（' + e + '），改为下载文件');
    }
  }

  // 回传失败时的兜底：下载 JSON 文件（手动模式可再 Ctrl+V 粘贴导入）
  const blob = new Blob([text], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'comments_' + platform + '_' + Date.now() + '.json';
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);

  console.log('%c[CommunityPulse] 采集完成，共 ' + payload.count + ' 条，已下载 comments_' + platform + '_*.json',
    'color:#16a34a;font-weight:bold');
  if (AUTO) {
    window.__CP_done = JSON.stringify({ ok: false, error: '本地服务回传失败，已下载 JSON 文件' });
  } else {
    // 顺手复制到剪贴板，回工具直接 Ctrl+V 粘贴即可
    try { await navigator.clipboard.writeText(text); console.log('已复制到剪贴板，可在「手动导入」直接 Ctrl+V。'); } catch (e) {}
    console.log('若浏览器拦截了本地回传，把 JSON 内容粘进 CommunityPulse 左侧「手动导入」即可分析。');
  }

  // 探测本地 CommunityPulse 服务端口（顺延端口机制下可能是 8766/8767/...）
  async function detectService() {
    for (const p of [8766, 8767, 8768, 8765]) {
      try {
        const r = await fetch('http://127.0.0.1:' + p + '/api/health', { method: 'GET' });
        const j = await r.json();
        if (j && j.ok) return 'http://127.0.0.1:' + p;
      } catch (e) { /* 端口没开或浏览器拦截了跨协议请求 */ }
    }
    return null;
  }
})();
