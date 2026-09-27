/* CommunityPulse 前端逻辑 */
const $ = (s) => document.querySelector(s);
const state = { taskId: null, result: null, tab: 'kw', charts: {} };

function toast(msg) {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 2600);
}
const esc = (s) => (s || '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const LBL = { positive: '正面', neutral: '中立', negative: '负面' };

async function api(path, opts) {
  const r = await fetch(path, opts);
  return await r.json();
}

/* ---------- 初始化 ---------- */
async function init() {
  const h = await api('/api/health');
  $('#health').textContent = h.ok ? `服务正常${h.jieba ? '' : '（未装 jieba，使用内置分词）'}` : '服务异常';
  const p = await api('/api/platforms');
  const rel = { stable: '稳定接口', 'best-effort': '网页解析', restricted: '风控受限', manual: '需手动导入' };
  $('#platList').innerHTML = p.items.map((i) =>
    `<span class="chip ${i.reliability}" title="${esc(i.hint)}">${esc(i.label)} · ${rel[i.reliability]}</span>`).join('');
  loadTasks();
  setInterval(loadTasks, 2500);
}

async function loadTasks() {
  const r = await api('/api/tasks');
  const box = $('#taskList');
  if (!r.items || !r.items.length) { box.innerHTML = '<span class="mut">暂无任务</span>'; return; }
  box.innerHTML = r.items.map((t) => `
    <div class="task ${state.taskId === t.id ? 'active' : ''}" data-id="${t.id}">
      <div class="t1">
        <b title="${esc(t.source_url || '')}">${esc(t.name || t.source_url || '未命名')}</b>
        <span class="status ${t.status}">${{ running: '进行中', done: '完成', error: '失败', pending: '排队' }[t.status] || t.status}</span>
      </div>
      <div class="t2" title="${esc(t.message || '')}">${esc(t.platform || '-')} · ${t.total || 0} 条 · ${esc((t.message || '').slice(0, 60))}</div>
      <div class="ops">
        <button class="sm" data-act="analyze" data-id="${t.id}">分析</button>
        <button class="sm" data-act="recollect" data-id="${t.id}" title="按同样来源再抓一次，只入库新增评论">重采</button>
        <button class="sm" data-act="del" data-id="${t.id}">删除</button>
      </div>
    </div>`).join('');
  box.querySelectorAll('.task').forEach((el) => {
    el.onclick = (e) => {
      const act = e.target.dataset.act;
      const id = +e.target.dataset.id || +el.dataset.id;
      if (act === 'del') { delTask(id); return; }
      if (act === 'recollect') { recollectTask(id); return; }
      if (act === 'analyze') { state.taskId = id; runAnalyze(id); return; }
      state.taskId = id;
      $('#curTask').textContent = '已选任务 #' + id;
      loadTasks();
    };
  });
}

async function recollectTask(id) {
  const r = await api(`/api/tasks/${id}/recollect`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ limit: +$('#limit').value || 300, sort: $('#sortMode').value }),
  });
  if (!r.ok) return toast(r.error || '无法重新采集');
  toast('已开始重新采集，只会入库新增评论');
  loadTasks();
}

/* ---------- 关键词发现 ---------- */
const discovered = new Map(); // url -> item

function fmtNum(v) {
  return v >= 10000 ? (v / 10000).toFixed(1) + '万' : String(v || 0);
}

$('#btnKeywordCollect').onclick = async () => {
  const kw = $('#kw').value.trim();
  if (!kw) return toast('请先填写关键词');
  const platform = $('#kwPlatform').value;
  const topN = +$('#kwTopN').value || 5;
  const comments = +$('#kwComments').value || 300;
  const sort = $('#kwSort').value;
  const box = $('#discoverBox');
  box.innerHTML = `<div class="mut">搜索「${esc(kw)}」并抓取热度前 ${topN} 个来源的评论（每个 ${comments} 条）…</div>`;
  const r = await api('/api/keyword-collect', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keyword: kw, platform, video_limit: topN,
                           comment_limit: comments, sort }),
  });
  if (!r.ok) { box.innerHTML = `<div class="err-box">${esc(r.error || '采集失败')}</div>`; return; }
  // 抖音 / 小红书 / TapTap：浏览器助手异步跑（搜索 + 逐条抓评论合并为一个任务），轮询进度
  if (r.browser) {
    box.innerHTML = `<div class="ok-box">已在已登录浏览器中搜索「${esc(kw)}」并逐条抓取评论（多来源合并为一个任务），请保持浏览器打开…</div>`;
    watchBrowserKeyword(r.job_id, box);
    return;
  }
  state.taskId = r.task_id;
  $('#curTask').textContent = '已选任务 #' + r.task_id;
  const vids = (r.videos || []).map((v, i) =>
    `<div class="disc-item"><span class="di-main"><b>${i + 1}. ${esc((v.title || '').slice(0, 42))}</b>` +
    `<span class="mut">${v.author ? esc(v.author) + ' · ' : ''}播放 ${fmtNum(v.play)}` +
    `${v.reply ? ' · 评论 ' + fmtNum(v.reply) : ''}</span></span></div>`).join('');
  box.innerHTML = `<div class="ok-box">已创建任务 #${r.task_id}：抓取「${esc(kw)}」在 ${esc(r.platform)} 的 <b>热度前 ${r.video_limit}</b> 个来源，每个 ${r.comment_limit} 条评论</div>` +
    `<div class="disc-list">${vids}</div><div class="mut">任务已在后台运行，下方任务列表可看实时进度。</div>`;
  loadTasks();
  watchAndAnalyze(r.task_id);
};

async function watchBrowserKeyword(job_id, box) {
  const tick = async () => {
    const r = await api('/api/browser-keyword-collect/' + job_id);
    if (!r.ok) { box.innerHTML = `<div class="err-box">${esc(r.error || '查询失败')}</div>`; return; }
    const tid = r.task_id || (r.result && r.result.task_id);
    if (r.status === 'running' || r.status === 'pending') {
      // 多来源合并进同一个任务，顺手把任务实时进度显示出来
      let prog = '';
      if (tid) {
        const t = await api('/api/tasks/' + tid);
        if (t.ok && t.task) {
          prog = `<div class="mut">任务 #${tid}：已入库 <b>${t.task.count || 0}</b> 条 · ` +
            `${esc((t.task.message || '').slice(0, 48))}</div>`;
        }
      }
      box.innerHTML = `<div class="mut">浏览器采集中…（请保持浏览器打开）</div>` + prog;
      setTimeout(tick, 3000); return;
    }
    const res = r.result || {};
    if (!res.ok) {
      box.innerHTML = `<div class="err-box">${esc(res.error || '搜索/采集失败')}</div>` +
        (tid ? `<div class="mut">已创建任务 #${tid}（失败），可在任务列表删除。</div>` : '');
      loadTasks();
      return;
    }
    state.taskId = res.task_id;
    $('#curTask').textContent = '已选任务 #' + res.task_id;
    const fails = res.fails || [];
    box.innerHTML = `<div class="ok-box">已在浏览器完成「${esc(res.keyword || '')}」的搜索与采集：` +
      `<b>${(res.urls || []).length}</b> 个来源合并为任务 #${res.task_id}，共入库 ` +
      `<b>${res.total || 0}</b> 条评论。</div>` +
      (fails.length ? `<div class="warn-box">部分来源未完成：${esc(fails.join('；'))}</div>` : '') +
      `<div class="mut">有效评论会随来源数累加，正在准备分析…</div>`;
    loadTasks();
    watchAndAnalyze(res.task_id);
  };
  setTimeout(tick, 1500);
}

$('#btnDiscover').onclick = async () => {
  const kw = $('#kw').value.trim();
  if (!kw) return toast('请先填写关键词');
  const box = $('#discoverBox');
  box.innerHTML = '<div class="mut">搜索中…</div>';
  const r = await api('/api/discover', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ keyword: kw, platform: $('#kwPlatform').value,
                           sort: $('#kwSort').value, limit: +$('#kwLimit').value || 20 }),
  });
  if (!r.ok) { box.innerHTML = `<div class="err-box">${esc(r.error || '搜索失败')}</div>`; return; }
  if (!r.count) {
    box.innerHTML = `<div class="warn-box">没有搜到结果。${esc(r.empty_hint || '换个关键词试试，或直接粘贴链接采集。')}</div>`;
    return;
  }
  const n = (v) => (v >= 10000 ? (v / 10000).toFixed(1) + '万' : String(v || 0));
  box.innerHTML = `
    <div class="ok-box">搜到 <b>${r.count}</b> 条${r.note ? ' · ' + esc(r.note) : ''}</div>
    <div class="disc-list">
      ${r.items.map((it, i) => `
        <label class="disc-item">
          <input type="checkbox" data-i="${i}" ${i < 3 ? 'checked' : ''}>
          <span class="di-main">
            <b>${esc(it.title.slice(0, 46))}</b>
            <span class="mut">
              ${it.author ? esc(it.author) + ' · ' : ''}
              播放 ${n(it.play)}${it.reply ? ' · 评论 ' + n(it.reply) : ''}
            </span>
          </span>
        </label>`).join('')}
    </div>
    <div class="row" style="margin-top:8px">
      <button id="btnUseDiscovered" class="primary">采集选中的来源</button>
      <button id="btnAppendDiscovered" class="ghost">追加到链接框</button>
    </div>`;
  r.items.forEach((it) => discovered.set(it.url, it));
  box.querySelectorAll('.disc-item input').forEach((cb) => {
    cb.onchange = () => { /* 勾选状态即选择 */ };
  });
  const picked = () => Array.from(box.querySelectorAll('.disc-item input:checked'))
    .map((cb) => r.items[+cb.dataset.i].url);
  $('#btnUseDiscovered').onclick = async () => {
    const urls = picked();
    if (!urls.length) return toast('请至少勾选一个');
    const c = await api('/api/tasks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ urls, name: kw + ' 相关舆情',
                             limit: +$('#limit').value || 300, sort: $('#sortMode').value }),
    });
    if (!c.ok) return toast(c.error || '创建失败');
    state.taskId = c.task_id;
    $('#curTask').textContent = '已选任务 #' + c.task_id;
    toast(`已开始批量采集 ${urls.length} 个来源`);
    loadTasks();
  };
  $('#btnAppendDiscovered').onclick = () => {
    const urls = picked();
    const cur = $('#url').value.trim();
    $('#url').value = (cur ? cur + '\n' : '') + urls.join('\n');
    toast(`已追加 ${urls.length} 条链接`);
  };
};

// 修复：原「仅搜索」按钮与上方搜索框共用 id，导致点击无响应；此处绑定同一处理函数
if ($('#btnDiscoverOnly')) { $('#btnDiscoverOnly').onclick = $('#btnDiscover').onclick; }

async function delTask(id) {
  if (!confirm('确认删除该任务及其评论？')) return;
  await fetch(`/api/tasks/${id}`, { method: 'DELETE' });
  if (state.taskId === id) { state.taskId = null; $('#curTask').textContent = '未选中任务'; }
  loadTasks();
}

/* ---------- 采集 ---------- */
function diagHtml(r) {
  // 抓取诊断：目标条数 vs 实得，没抓够时说明原因
  const want = r.requested || 0;
  const got = r.count || 0;
  if (!want) return '';
  // 平台侧评论总量（接口有返回时），让用户知道"实得"占全量的比例
  const avail = r.available || 0;
  const scale = avail ? ` <span class="mut">（该来源评论总量 ${avail} 条，本次覆盖 ${Math.round(got / avail * 100)}%）</span>` : '';
  if (got >= want) {
    let tip = r.capacity_hint ? ' · ' + esc(r.capacity_hint) : '';
    if (avail && got < avail) {
      tip += ' · 想要更多可把目标条数调大';
    }
    return `<div class="ok-box">目标 <b>${want}</b> 条，实得 <b>${got}</b> 条 ✓ 已抓满${scale}${tip}</div>`;
  }
  return `<div class="warn-box">目标 <b>${want}</b> 条，实得 <b>${got}</b> 条 —— 没抓满。${scale}<br>
    原因：${esc(r.stop_reason || '来源评论量不足')}
    ${r.capacity_hint ? '<br>' + esc(r.capacity_hint) : ''}
    <br>可尝试：把目标条数调低、换「热度+最新合并」，或在设置里填该平台 Cookie。</div>`;
}

/** 文本框里每行一个来源；超长链接被换行/空格截断会自动合并成单个有效链接 */
function urlList() {
  const text = $('#url').value || '';
  const lines = text.split(/\n+/).map((s) => s.trim()).filter(Boolean);
  // 先把「被换行切断的长链接」合并：以 http 开头的行作为链接起点，
  // 后续不含 http、但全是 URL 合法字符的续行，拼回上一个链接
  const merged = [];
  for (const line of lines) {
    if (/^https?:\/\//i.test(line)) {
      merged.push(line);
    } else if (merged.length && /^https?:\/\//i.test(merged[merged.length - 1])
               && /^[\w\-./?=&%#@:+]+$/.test(line) && !/^www\./i.test(line)) {
      merged[merged.length - 1] += line;
    } else {
      merged.push(line); // 普通说明文字，后续靠正则抽取内嵌链接
    }
  }
  const out = [];
  for (const raw of merged) {
    let line = raw;
    // 行内含 http 链接时，去掉行内所有空白：修复超长链接被复制时插入的空格
    if (/https?:\/\//i.test(line)) {
      const compact = line.replace(/\s+/g, '');
      // 仅当压缩后仍是「单个链接」才采用，避免误把多个链接并成一个
      if ((compact.match(/https?:\/\//gi) || []).length === 1) line = compact;
    }
    // 用正则抽取所有 http(s):// 片段（仅 URL 合法字符，遇到中文/其他字符即止）
    const ms = line.match(/https?:\/\/[\w\-./?=&%#@:+~]+/gi) || [];
    for (const m of ms) {
      const u = m.replace(/[)\]}>」』,，。、]+$/, '').trim();
      if (/^https?:\/\/\S+$/i.test(u)) out.push(u);
    }
  }
  // 去重并保持顺序
  const seen = new Set();
  return out.filter((u) => (seen.has(u) ? false : (seen.add(u), true)));
}

/** 实时显示已识别的链接数量，便于确认长链接没被误判成多个 */
function updateUrlCount() {
  const n = urlList().length;
  const el = document.getElementById('urlCount');
  if (!el) return;
  if (n === 0) {
    el.textContent = '尚未识别到链接';
    el.style.color = '';
  } else {
    el.textContent = `已识别 ${n} 个链接` + (n > 1 ? '（将作为多来源批量采集）' : '');
    el.style.color = 'var(--brand)';
  }
}

$('#btnPreview').onclick = async () => {
  const url = urlList()[0] || '';
  if (!url) return toast('请先填写链接');
  const want = +$('#limit').value || 300;
  $('#previewBox').innerHTML = '<div class="mut">试抓中…</div>';
  const r = await api('/api/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, limit: want, sort: $('#sortMode').value }),
  });
  if (!r.ok) {
    $('#previewBox').innerHTML = `<div class="err-box"><b>${esc(r.label || '')} 抓取失败</b><br>${esc(r.error)}<br>${esc(r.hint || '')}</div>`;
    return;
  }
  const rel = { stable: '稳定接口', 'best-effort': '网页解析', restricted: '风控受限', manual: '需手动导入' };
  const samples = (r.samples || []).slice(0, 5).map((s) =>
    `<div class="sample">${esc((s.user_name ? s.user_name + '：' : '') + s.content).slice(0, 160)}</div>`).join('');
  $('#previewBox').innerHTML =
    `<div class="ok-box">识别为 <b>${esc(r.label)}</b>（${rel[r.reliability]}）${r.title ? ' · ' + esc(r.title) : ''}</div>
     ${diagHtml(r)}
     ${samples || '<div class="mut">没有拿到样本</div>'}
     ${(r.warnings || []).map((w) => `<div class="warn-box">${esc(w)}</div>`).join('')}`;
};

$('#btnCollect').onclick = async () => {
  const urls = urlList();
  if (!urls.length) return toast('请先填写链接（每行一个）');
  const u = urls[0];
  // 抖音 / 小红书 有签名风控，服务端直连抓不到，走一键浏览器采集闭环
  if (/douyin\.com|iesdouyin\.com|xiaohongshu\.com|xhslink\.com/.test(u)) {
    if (urls.length > 1) {
      return toast('抖音 / 小红书请一次只采集一个链接（用浏览器采集助手）');
    }
    const platform = /douyin/.test(u) ? 'douyin' : 'xiaohongshu';
    return startBrowserCapture(u, platform);
  }
  const r = await api('/api/tasks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ urls, name: $('#name').value.trim(),
                           limit: +$('#limit').value || 300, sort: $('#sortMode').value }),
  });
  if (!r.ok) return toast(r.error || '创建失败');
  state.taskId = r.task_id;
  $('#curTask').textContent = '已选任务 #' + r.task_id;
  toast(r.source_count > 1
    ? `已开始批量采集 ${r.source_count} 个来源，每个目标 ${r.limit} 条`
    : `已开始采集，目标 ${r.limit} 条`);
  loadTasks();
  watchAndAnalyze(r.task_id);
};

/** 一键浏览器采集：拉起已登录浏览器 → 注入脚本 → 滚动抓完 → 自动回传入库。 */
async function startBrowserCapture(url, platform) {
  const btn = $('#btnCollect');
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '正在启动浏览器采集…';
  const r = await api('/api/browser-capture', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, platform, target: +$('#limit').value || 300 }),
  });
  if (!r.ok) {
    btn.disabled = false; btn.textContent = old;
    return toast(r.error || '启动失败');
  }
  toast('已拉起浏览器，正在自动采集（请勿关闭弹出的浏览器窗口）');
  const timer = setInterval(async () => {
    const j = await api('/api/browser-capture/' + r.job_id);
    if (!j.ok) {
      clearInterval(timer); btn.disabled = false; btn.textContent = old;
      return toast(j.error || '查询失败');
    }
    if (j.status === 'running' || j.status === 'pending') return;
    // done
    clearInterval(timer); btn.disabled = false; btn.textContent = old;
    const res = j.result || {};
    if (res.ok) {
      state.taskId = res.task_id;
      $('#curTask').textContent = '已选任务 #' + res.task_id;
      toast(`采集完成：${res.count || 0} 条，已生成任务 #${res.task_id}`);
      loadTasks();
      runAnalyze(res.task_id);
    } else {
      toast('浏览器采集未拿到数据：' + (res.error || '') +
        '\n若提示未登录，请在弹出的浏览器里登录后重新点「采集」。');
      loadTasks();
    }
  }, 3000);
}

function watchAndAnalyze(id) {
  const timer = setInterval(async () => {
    const r = await api(`/api/tasks/${id}`);
    const t = r.task;
    if (!t || t.status === 'done' || t.status === 'error') {
      clearInterval(timer);
      loadTasks();
      if (t && t.status === 'done' && (t.count || 0) > 0) {
        toast('采集结束：' + (t.message || ''));
        runAnalyze(id);
      } else if (t) {
        toast('采集结束：' + (t.message || ''));
      }
    }
  }, 2000);
}

$('#btnImport').onclick = async () => {
  const text = $('#impText').value.trim();
  if (!text) return toast('请先粘贴内容');
  const r = await api('/api/import', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, fmt: $('#impFmt').value }),
  });
  if (!r.ok) return toast(r.error || '导入失败');
  state.taskId = r.task_id;
  $('#curTask').textContent = '已选任务 #' + r.task_id;
  toast(`导入成功 ${r.count} 条`);
  loadTasks();
  runAnalyze(r.task_id);
};

$('#btnCopyScript').onclick = async () => {
  const btn = $('#btnCopyScript');
  try {
    const res = await fetch('/snippets/browser_capture.js');
    if (!res.ok) throw new Error('读取脚本失败 ' + res.status);
    const code = await res.text();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(code);
    } else {
      // file:// 或非安全上下文下 clipboard 不可用，退回文本框选中
      const ta = document.createElement('textarea');
      ta.value = code; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
    }
    btn.textContent = '已复制到剪贴板 ✓';
    toast('已复制。到目标页面 F12 → Console 粘贴回车即可');
    setTimeout(() => { btn.textContent = '复制浏览器采集脚本（抖音 / 小红书）'; }, 2500);
  } catch (e) {
    toast('复制失败：' + e.message + '。也可直接打开 web\\snippets\\browser_capture.js 手动复制');
  }
};

/* ---------- 分析 ---------- */
$('#btnAnalyze').onclick = () => {
  if (!state.taskId) return toast('请先在左侧选中或采集一个任务');
  runAnalyze(state.taskId);
};

async function runAnalyze(id) {
  const payload = {
    cluster_threshold: +$('#threshold').value || 0.45,
    top_n: +$('#topN').value || 60,
    extra_stopwords: $('#stopwords').value.trim(),
    top_liked_n: +$('#topLiked').value || 0,
  };
  toast('分析中…');
  let r = await api(`/api/analyze/${id}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  // 采集还在进行：不静默按当前数据出结果，先说清楚代价再让用户决定
  if (!r.ok && r.code === 'collecting') {
    const tgt = r.target ? ` / 目标 ${r.target} 条` : '';
    const go = confirm(
      `任务 #${id} 还在采集中（已入库 ${r.collected} 条${tgt}）。\n\n` +
      `现在分析只会覆盖这部分数据，采完后需要重新分析才能得到全量结果。\n\n` +
      `仍要分析当前数据吗？`);
    if (!go) { loadTasks(); return; }
    r = await api(`/api/analyze/${id}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...payload, force: true }),
    });
  }
  if (!r.ok) return toast(r.error || '分析失败');
  state.result = r.result;
  state.taskId = id;
  state.tab = 'kw';
  render();
  const s = r.result.stats || {};
  if (s.task_total && s.raw_total && s.raw_total < s.task_total) {
    toast(`分析完成：本次分析 ${s.raw_total} 条 / 任务共 ${s.task_total} 条（有效 ${s.total} 条）`);
  } else {
    toast('分析完成');
  }
}

/* ---------- 渲染 ---------- */
function render() {
  const d = state.result;
  if (!d || !d.stats || !d.stats.total) {
    $('#result').innerHTML = '<div class="empty">' + esc((d && d.error) || '暂无分析结果') + '</div>';
    return;
  }
  const s = d.stats;
  // 用 raw_total（本次实际读取条数）与任务库中总数比较：total 是去重后的，拿它比会误判
  const cut = s.task_total && s.raw_total && s.raw_total < s.task_total;
  const useTop = s.top_liked_n && s.pool_size && s.top_liked_n < s.pool_size;
  const kpi = `
    <div class="kpis">
      <div class="kpi"><b>${s.total}</b><span>${cut ? '本次有效评论' : '有效评论'}</span></div>
      <div class="kpi"><b>${(s.positive_rate * 100).toFixed(0)}%</b><span>好评率</span></div>
      <div class="kpi"><b style="color:#d9534f">${(s.negative_rate * 100).toFixed(0)}%</b><span>差评率</span></div>
      <div class="kpi"><b>${(d.clusters || []).length}</b><span>相似句族群</span></div>
      <div class="kpi"><b>${(d.topics || []).length}</b><span>吐槽点类别</span></div>
    </div>`;
  const cutReason = useTop
    ? '「仅分析热度前 N 条」截取了数据，改成 0 可看全部。'
    : '通常是采集尚未结束就开始分析导致的；等任务显示「完成」后点「重新分析」即可得到全量。';
  const scopeTip = cut
    ? `<div class="warn-box">本次仅分析 <b>${s.raw_total} 条</b> / 任务共 <b>${s.task_total} 条</b>（有效 ${s.total} 条）。${cutReason}</div>`
    : (useTop
      ? `<div class="warn-box">当前只分析<b>热度前 ${s.top_liked_n} 条</b>（共 ${s.pool_size} 条）。
         想看全量，把「仅分析热度前 N 条」改成 0 重新分析。</div>`
      : (s.pool_size ? `<div class="mut" style="margin-bottom:10px">分析范围：全部 ${s.pool_size} 条</div>` : ''));
  const noteHtml = (d.notes || []).length
    ? `<div class="warn-box">${(d.notes || []).map(esc).join('<br>')}</div>` : '';
  const tabs = [
    ['kw', '高频词条'], ['sim', '相似句聚类'], ['topic', '吐槽点归类'],
    ['sent', '情感分布'], ['trend', '声量趋势'], ['hl', '重点原句'],
    ['cm', '评论逐条'],
  ];
  $('#result').innerHTML = kpi + `
    <div class="panel">
      ${scopeTip}${noteHtml}
      <div class="tabs">${tabs.map(([k, n]) => `<button data-tab="${k}" class="${state.tab === k ? 'active' : ''}">${n}</button>`).join('')}</div>
      <div id="tabBody"></div>
    </div>`;
  $('#result').querySelectorAll('.tabs button').forEach((b) => {
    b.onclick = () => { state.tab = b.dataset.tab; render(); };
  });
  renderTab();
}

function renderTab() {
  const d = state.result, body = $('#tabBody');
  ({ kw: tabKeywords, sim: tabSimilar, topic: tabTopics, sent: tabSentiment,
     trend: tabTrend, hl: tabHighlights, cm: tabComments })[state.tab](body, d);
  _bindStmtChips(body);   // 评论逐条是异步渲染，内部会再绑一次
}

// 词条情感记忆：标签循环顺序
const SENT_CYCLE = ["neutral", "positive", "negative"];

function _kwSentLabel(k) {
  // 优先显示用户记忆标签，否则显示默认词典自动判定
  return k.memory_label || k.auto_label || "neutral";
}

function _sentChipHtml(k) {
  const lbl = _kwSentLabel(k);
  const tip = k.memory_label
    ? `已记住为「${LBL[lbl]}」（点击切换）`
    : `自动判定「${LBL[lbl]}」，点击可改并记忆`;
  return `<span class="sent-chip ${lbl}" data-w="${esc(k.word)}" title="${tip}">${LBL[lbl]}</span>`;
}

async function _cycleSentiment(word, el) {
  const cur = el.className.includes("positive") ? "positive"
            : el.className.includes("negative") ? "negative" : "neutral";
  const next = SENT_CYCLE[(SENT_CYCLE.indexOf(cur) + 1) % SENT_CYCLE.length];
  el.className = `sent-chip ${next}`;
  el.textContent = LBL[next];
  try {
    await api('/api/sentiment-memory', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ word, label: next }),
    });
    el.title = next === "neutral"
      ? `已改为「中立」并记忆（下次分析生效）`
      : `已记住为「${LBL[next]}」（下次分析生效）`;
  } catch (e) {
    toast('记忆保存失败：' + (e && e.message ? e.message : e));
  }
}

/* ---------- 语句 / 评论级情感记忆 ----------
   标签落在「这一条评论」上：按正文指纹记忆，下次重新分析、
   或从别的来源再采到同样的内容时，会直接沿用你的标注。 */

function _stmtChipHtml(x) {
  const lbl = x.mem_label || x.sentiment || 'neutral';
  const tip = x.mem_label
    ? `已记住这条评论为「${LBL[lbl]}」（点击切换）`
    : `自动判定「${LBL[lbl]}」，点击可改并记住这条评论`;
  const attr = x.key ? `data-key="${esc(x.key)}"` : '';
  const cls = x.mem_label ? 'memorized' : '';
  return `<span class="sent-chip ${lbl} ${cls}" ${attr} title="${tip}">${LBL[lbl]}</span>`;
}

async function _cycleStmt(key, el) {
  if (!key) return toast('这条评论缺少指纹，无法记忆');
  const cur = el.className.includes("positive") ? "positive"
            : el.className.includes("negative") ? "negative" : "neutral";
  const next = SENT_CYCLE[(SENT_CYCLE.indexOf(cur) + 1) % SENT_CYCLE.length];
  el.className = `sent-chip ${next}`;
  el.textContent = LBL[next];
  try {
    await api('/api/comment-sentiment', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, label: next }),
    });
    el.classList.add('memorized');
    el.title = `已记住这条评论为「${LBL[next]}」，重新分析后统计会更新`;
    toast('已记住这条评论的情感标签，重新分析后统计随之更新');
  } catch (e) {
    toast('记忆保存失败：' + (e && e.message ? e.message : e));
  }
}

function _bindStmtChips(root) {
  root.querySelectorAll('.sent-chip[data-key]').forEach((el) => {
    el.onclick = (ev) => { ev.stopPropagation(); _cycleStmt(el.dataset.key, el); };
  });
}

function tabKeywords(body, d) {
  const top = (d.keywords || []).slice(0, 40);
  body.innerHTML = `
    <div id="kwChart" class="chart"></div>
    <h3 style="margin:16px 0 8px;font-size:14px">词条明细 <span class="tip">点击词条看原句；点击右侧标签可改情感并记忆</span></h3>
    <div class="kw-grid">${top.map((k) =>
      `<div class="kw" data-w="${esc(k.word)}"><b>${esc(k.word)}</b>${_sentChipHtml(k)}<span>${k.docs} 条</span></div>`).join('')}</div>
    ${d.new_words && d.new_words.length ? `<h3 style="margin:18px 0 8px;font-size:14px">疑似新词 / 黑话 <span class="tip">按凝固度识别</span></h3>
      <div class="kw-grid">${d.new_words.slice(0, 24).map((n) =>
        `<div class="kw"><b>${esc(n.word)}</b><span>${n.count} 次</span></div>`).join('')}</div>` : ''}
    ${d.phrases && d.phrases.length ? `<h3 style="margin:18px 0 8px;font-size:14px">高频短语</h3>
      <div class="kw-grid">${d.phrases.slice(0, 24).map((p) =>
        `<div class="kw"><b>${esc(p.phrase)}</b><span>${p.count} 次</span></div>`).join('')}</div>` : ''}
    <div id="kwEx" style="margin-top:14px"></div>`;
  const chart = echarts.init(document.getElementById('kwChart'));
  const data = top.slice(0, 20);
  chart.setOption({
    grid: { left: 90, right: 50, top: 10, bottom: 20 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f5' } } },
    yAxis: { type: 'category', data: data.map((k) => k.word).reverse(),
      axisLine: { lineStyle: { color: '#dfe3e8' } }, axisLabel: { color: '#5b636b' } },
    series: [{
      type: 'bar', data: data.map((k) => k.docs).reverse(), barMaxWidth: 16,
      itemStyle: { color: '#3b6fd4', borderRadius: [0, 4, 4, 0] },
      label: { show: true, position: 'right', color: '#8b9299', fontSize: 11 },
    }],
  });
  state.charts.kw = chart;
  body.querySelectorAll('.kw').forEach((el) => {
    el.style.cursor = 'pointer';
    el.onclick = (ev) => {
      if (ev.target.classList.contains('sent-chip')) {
        ev.stopPropagation();
        _cycleSentiment(el.dataset.w, ev.target);
        return;
      }
      showKeywordExamples(el.dataset.w, d);
    };
  });
}

function showKeywordExamples(word, d) {
  const hits = [];
  const push = (arr) => (arr || []).forEach((x) => {
    if ((x.content || '').includes(word)) hits.push(x);
  });
  push(d.highlights);
  (d.clusters || []).forEach((c) => { push(c.samples); if ((c.representative || '').includes(word)) hits.push({ content: c.representative, like_count: c.rep_meta.like_count }); });
  (d.topics || []).forEach((t) => push(t.samples));
  push(d.sentiment_examples.negative); push(d.sentiment_examples.positive);
  const box = $('#kwEx');
  box.innerHTML = `<div class="card"><div class="ch"><b>包含「${esc(word)}」的原句</b>
    <span class="mut">${hits.length} 条</span></div>
    ${hits.slice(0, 12).map((h) => `<div class="sub">${esc((h.content || '').slice(0, 180))} <span class="mut">👍${h.like_count || 0}</span></div>`).join('')
    || '<div class="mut">已分析样本中没有完整原句，可在导出的评论明细里检索该词</div>'}</div>`;
}

function tabSimilar(body, d) {
  const cs = d.clusters || [];
  body.innerHTML = cs.length ? cs.map((c) => `
    <div class="card">
      <div class="ch">
        <b>相似 ${c.size} 条</b>
        ${_stmtChipHtml(c.rep_meta)}
        <span class="mut">总点赞 ${c.total_likes}</span>
        <span class="mut">负 ${c.sentiment.negative} / 中 ${c.sentiment.neutral} / 正 ${c.sentiment.positive}</span>
        <span class="mut">${esc((c.keywords || []).join('、'))}</span>
        <span class="mut">${esc((c.topics || []).join('、'))}</span>
      </div>
      <div class="rep">${esc(c.representative)}</div>
      ${c.samples.slice(0, 4).map((s) => `<div class="sub">${_stmtChipHtml(s)} ${esc(s.content)} <span class="mut">👍${s.like_count} · ${esc(s.user || '')}</span></div>`).join('')}
    </div>`).join('') : '<div class="empty">没有形成相似句族群，可调低阈值后重新分析</div>';
}

function tabTopics(body, d) {
  const ts = d.topics || [];
  body.innerHTML = `
    <div id="tpChart" class="chart sm"></div>
    ${ts.map((t) => `
    <div class="card">
      <div class="ch">
        <b>${esc(t.topic)}</b><span class="mut">${t.count} 条</span>
        <span class="tag ${t.neg_rate > 0.5 ? 'negative' : t.neg_rate > 0.25 ? 'neutral' : 'positive'}">负面率 ${(t.neg_rate * 100).toFixed(0)}%</span>
        <span class="mut">${esc((t.keywords || []).join('、'))}</span>
      </div>
      ${t.samples.slice(0, 3).map((s) => `<div class="sub">${_stmtChipHtml(s)} ${esc(s.content)} <span class="mut">👍${s.like_count}</span></div>`).join('')}
    </div>`).join('')}`;
  const chart = echarts.init(document.getElementById('tpChart'));
  chart.setOption({
    grid: { left: 110, right: 60, top: 10, bottom: 20 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'value', splitLine: { lineStyle: { color: '#eef1f5' } } },
    yAxis: { type: 'category', data: ts.map((t) => t.topic).reverse(), axisLabel: { color: '#5b636b' } },
    series: [{
      type: 'bar', data: ts.map((t) => t.count).reverse(), barMaxWidth: 16,
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: (p) => ['#4c9a5b', '#e0a33e', '#d9534f'][
          ts.slice().reverse()[p.dataIndex].neg_rate > 0.5 ? 2 : ts.slice().reverse()[p.dataIndex].neg_rate > 0.25 ? 1 : 0],
      },
      label: { show: true, position: 'right', color: '#8b9299', fontSize: 11 },
    }],
  });
  state.charts.tp = chart;
}

function tabSentiment(body, d) {
  const s = d.stats;
  body.innerHTML = `
    <div id="stChart" class="chart sm"></div>
    <div class="row" style="gap:14px;align-items:flex-start;margin-top:10px">
      <div style="flex:1">
        <h3 style="font-size:14px;margin:0 0 8px">最负面原声 <span class="tip">点左侧标签可改情感并记住这条评论</span></h3>
        ${(d.sentiment_examples.negative || []).slice(0, 8).map((x) =>
          `<div class="sub" style="margin-bottom:8px">${_stmtChipHtml(x)} ${esc(x.content)} <span class="mut">得分 ${x.score} · 👍${x.like_count}</span></div>`).join('')}
      </div>
      <div style="flex:1">
        <h3 style="font-size:14px;margin:0 0 8px">最正面原声 <span class="tip">点左侧标签可改情感并记住这条评论</span></h3>
        ${(d.sentiment_examples.positive || []).slice(0, 8).map((x) =>
          `<div class="sub" style="margin-bottom:8px">${_stmtChipHtml(x)} ${esc(x.content)} <span class="mut">得分 ${x.score} · 👍${x.like_count}</span></div>`).join('')}
      </div>
    </div>`;
  const chart = echarts.init(document.getElementById('stChart'));
  chart.setOption({
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: '#5b636b' } },
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '45%'],
      label: { formatter: '{b} {c} ({d}%)', color: '#5b636b' },
      data: [
        { name: '正面', value: s.positive, itemStyle: { color: '#4c9a5b' } },
        { name: '中立', value: s.neutral, itemStyle: { color: '#9aa0a6' } },
        { name: '负面', value: s.negative, itemStyle: { color: '#d9534f' } },
      ],
    }],
  });
  state.charts.st = chart;
}

function tabTrend(body, d) {
  const tr = d.trend || {};
  if (!tr.available) {
    body.innerHTML = `<div class="empty">${esc(tr.reason || '评论缺少时间戳，无法绘制趋势')}
      <div class="mut" style="margin-top:8px">B站 / Steam / Reddit / 微博 等接口会带时间；手动导入时可用 CSV 的 time 列补上</div></div>`;
    return;
  }
  body.innerHTML = '<div id="trChart" class="chart"></div>';
  const chart = echarts.init(document.getElementById('trChart'));
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { bottom: 0, textStyle: { color: '#5b636b' } },
    grid: { left: 50, right: 60, top: 20, bottom: 40 },
    xAxis: { type: 'category', data: tr.points.map((p) => p.date), axisLabel: { color: '#5b636b' } },
    yAxis: [
      { type: 'value', name: '声量', splitLine: { lineStyle: { color: '#eef1f5' } } },
      { type: 'value', name: '负面率', axisLabel: { formatter: '{value}%', color: '#5b636b' }, splitLine: { show: false } },
    ],
    series: [
      { name: '评论数', type: 'line', smooth: true, data: tr.points.map((p) => p.count),
        itemStyle: { color: '#3b6fd4' }, areaStyle: { color: 'rgba(59,111,212,.12)' } },
      { name: '负面率', type: 'line', yAxisIndex: 1, smooth: true,
        data: tr.points.map((p) => +(p.neg_rate * 100).toFixed(1)), itemStyle: { color: '#d9534f' } },
    ],
  });
  state.charts.tr = chart;
}

function tabHighlights(body, d) {
  const hs = d.highlights || [];
  body.innerHTML = hs.length ? `<div class="mut" style="margin-bottom:8px">点左侧标签可改这条评论的情感并记住它。</div>` + hs.map((h) => `
    <div class="card">
      <div class="ch">
        ${_stmtChipHtml(h)}
        <span class="reason">${esc(h.reason)}</span>
        <span class="mut">👍${h.like_count}</span>
        <span class="mut">${esc(h.user || '')}</span>
      </div>
      <div class="rep">${esc(h.content)}</div>
    </div>`).join('') : '<div class="empty">暂无</div>';
}

/* ---------- 评论逐条（逐条改标签并记忆） ---------- */
async function tabComments(body, d) {
  const tid = state.taskId;
  if (!tid) { body.innerHTML = '<div class="empty">请先在左侧选中一个任务</div>'; return; }
  body.innerHTML = '<div class="mut">正在加载评论…</div>';
  let r;
  try {
    r = await api(`/api/comments/${tid}?limit=300`);
  } catch (e) {
    body.innerHTML = `<div class="err-box">加载失败：${esc(e && e.message ? e.message : e)}</div>`;
    return;
  }
  if (!r.ok) { body.innerHTML = `<div class="err-box">${esc(r.error || '加载失败')}</div>`; return; }
  const items = r.items || [];
  const marked = items.filter((x) => x.mem_label).length;
  body.innerHTML = `
    <div class="mut" style="margin-bottom:10px;line-height:1.7">
      共 ${r.total} 条，按点赞显示前 ${items.length} 条。点每条左侧标签可改情感并<b>记住这条评论</b>；
      当前已记住 ${marked} 条。改完后重新分析，分布与统计会按你的标注更新。
    </div>
    <div class="cm-list">${items.map((x) => `
      <div class="cm-item">
        ${_stmtChipHtml(x)}
        <span class="cm-text">${esc(x.content)}</span>
        <span class="mut">👍${x.like_count}${x.user_name ? ' · ' + esc(x.user_name) : ''}</span>
      </div>`).join('')}</div>`;
  _bindStmtChips(body);
}

/* ---------- 导出 ---------- */
async function doExport(fmt) {
  if (!state.taskId) return toast('请先选中任务');
  const r = await api(`/api/export/${state.taskId}?fmt=${fmt}`);
  if (!r.ok) return toast(r.error || '导出失败');
  window.open(r.url, '_blank');
  toast('已导出：' + r.name);
}
$('#btnXlsx').onclick = () => doExport('xlsx');
$('#btnCsv').onclick = () => doExport('csv');
$('#btnHtml').onclick = () => doExport('html');

/* ---------- 平台接入（登录 → 一键获取凭据） ---------- */
const STATE_TXT = { linked: '已接入', none: '未接入', free: '免登录' };

function renderCreds(items, browser) {
  $('#credList').innerHTML = items.map((a) => {
    const linked = a.state === 'linked' || a.state === 'free';
    const watching = a.in_progress;
    const badge = watching ? '等待登录…' : STATE_TXT[a.state];
    const badgeCls = watching ? 'watching' : a.state;
    const btns = a.kind === 'none'
      ? `<button class="sm" data-act="home">打开站点</button>`
      : `
        <button class="sm" data-act="open">打开登录页</button>
        <button class="sm primary" data-act="capture">一键获取${a.kind === 'key' ? '' : ' Cookie'}</button>
        <button class="sm" data-act="paste">手动粘贴</button>
        ${a.state === 'linked' ? '<button class="sm" data-act="clear">清除</button>' : ''}`;
    const c2 = watching
      ? '已打开登录页，请在该网站完成登录，工具会<em>自动</em>读取登录态（保持浏览器打开直到此处变成「已接入」）'
      : (a.state === 'free' ? esc(a.no_login)
          : `未登录：${esc(a.no_login)}<br>登录后可：<em>${esc(a.after_login)}</em>`);
    return `<div class="cred ${badgeCls}" data-key="${esc(a.key)}">
      <div class="c1"><span>${esc(a.label)}</span>
        <span class="badge ${badgeCls}">${badge}</span>
        <span class="spacer"></span></div>
      <div class="c2">${c2}</div>
      <div class="c3">${btns}</div>
      ${a.masked ? `<div class="c4">当前：${esc(a.masked)}</div>` : ''}
      ${a.capture_error ? `<div class="c4 err">${esc(a.capture_error)}</div>` : ''}
    </div>`;
  }).join('') + (browser ? '' : '<div class="mut" style="margin-top:6px">提示：点「打开登录页」会自动拉起带调试端口的 Chrome/Edge；若未安装浏览器，请用「手动粘贴」。</div>');

  $('#credList').querySelectorAll('.cred').forEach((el) => {
    el.querySelectorAll('button').forEach((b) => {
      b.onclick = () => credAction(el.dataset.key, b.dataset.act);
    });
  });
}

let _watchTimer = null;
async function startWatchPoll() {
  clearInterval(_watchTimer);
  _watchTimer = setInterval(async () => {
    const c = await api('/api/credentials');
    if (!c || !c.ok) return;
    state.creds = c.items || [];
    renderCreds(state.creds, c.browser);
    const stillWatching = (state.creds || []).some((x) => x.in_progress);
    if (!stillWatching) {
      clearInterval(_watchTimer);
      const errs = (state.creds || []).map((x) => x.capture_error).filter(Boolean);
      if (errs.length) toast(errs[0], true);
      else if ((state.creds || []).some((x) => x.state === 'linked')) toast('登录态已自动保存');
    }
  }, 2500);
}

async function credAction(key, act) {
  const item = (state.creds || []).find((x) => x.key === key);
  if (!item) return;
  if (act === 'home') { window.open(item.home_url, '_blank'); return; }
  if (act === 'open') {
    toast('正在打开登录页，登录后请保持浏览器打开，工具会自动读取登录态…');
    const r = await api('/api/credentials/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: key }),
    });
    if (!r.ok) return toast(r.error || '打开失败');
    if (r.manual) { window.open(item.login_url, '_blank'); toast(r.message || '已打开申请页'); return; }
    startWatchPoll();
    return;
  }
  if (act === 'capture') {
    if (item.kind === 'key') { window.open(item.login_url, '_blank'); return credAction(key, 'paste'); }
    toast('正在从浏览器读取 Cookie…');
    const r = await api('/api/credentials/capture', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: key }),
    });
    if (!r.ok) return toast(r.error || '获取失败');
    toast(r.message || '已保存登录态');
    return loadSettings();
  }
  if (act === 'paste') {
    const v = prompt(`${item.label}：粘贴 Cookie${item.kind === 'key' ? ' 或 API Key' : ''}\n\n${item.sample || ''}`, '');
    if (v === null) return;
    await api('/api/credentials/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: key, value: v.trim() }),
    });
    toast('已保存');
    return loadSettings();
  }
  if (act === 'clear') {
    await api('/api/credentials/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ platform: key }),
    });
    toast('已清除');
    return loadSettings();
  }
}

async function loadSettings() {
  const r = await api('/api/settings');
  const s = r.settings || {};
  $('#setSteamLang').value = s.steam_language || 'all';
  $('#setSteamDays').value = s.steam_day_range || 365;

  const c = await api('/api/credentials');
  state.creds = c.items || [];
  renderCreds(state.creds, c.browser);
}
$('#btnReloadSet').onclick = loadSettings;
$('#btnCloseBrowser').onclick = async () => {
  const r = await api('/api/credentials/close', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  });
  toast(r.ok ? (r.message || '已关闭') : (r.error || '关闭失败'));
  loadSettings();
};
$('#btnSaveSet').onclick = async () => {
  await api('/api/settings', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      settings: {
        steam_language: $('#setSteamLang').value,
        steam_day_range: +$('#setSteamDays').value || 365,
      },
    }),
  });
  toast('设置已保存，下次采集生效');
};

loadSettings();
init();

// 链接文本框：实时显示识别到的链接数量，并自动撑高
(function bindUrlBox() {
  const ta = document.getElementById('url');
  if (!ta) return;
  const grow = () => {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 360) + 'px';
    updateUrlCount();
  };
  ta.addEventListener('input', grow);
  grow();
})();
