/* ============================================================
   本地运维助手 · 前端逻辑
   纯原生 JS，无构建步骤
   ============================================================ */
'use strict';

const $ = (id) => document.getElementById(id);
const api = {
  async get(path) { const r = await fetch(path); return r.json(); },
  async post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    return r.json();
  },
  async del(path) { const r = await fetch(path, { method: 'DELETE' }); return r.json(); }
};

const state = {
  sessions: [],
  sessionId: null,
  messages: [],
  mode: 'auto',
  settings: null,
  streaming: false,
  abort: null,
  drafts: []
};

const QUICK_CHIPS = [
  'Pod 一直 CrashLoopBackOff 怎么排查？',
  'MySQL 主从延迟很大怎么定位？',
  '磁盘 inode 满了怎么办？',
  'K8s Service 访问不通的排查思路',
  'Redis 大 key 怎么治理？',
  '如何设计一套 SLO 与告警？'
];

/* ==================== 工具 ==================== */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toast(msg, kind = 'info', ms = 3200) {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.innerHTML = msg;
  $('toastWrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(20px)'; }, ms - 300);
  setTimeout(() => el.remove(), ms);
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(String(ts).replace(' ', 'T'));
  if (isNaN(d)) return ts;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const p = (n) => String(n).padStart(2, '0');
  return sameDay ? `${p(d.getHours())}:${p(d.getMinutes())}`
                 : `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* ==================== Markdown ==================== */
marked.setOptions({ gfm: true, breaks: true, headerIds: false, mangle: false });

function renderMarkdown(text) {
  let html = '';
  try { html = marked.parse(text || ''); } catch (e) { html = '<p>' + esc(text) + '</p>'; }
  return html;
}

function decorate(root) {
  root.querySelectorAll('pre code').forEach((block) => {
    try { hljs.highlightElement(block); } catch (e) { /* ignore */ }
  });
  root.querySelectorAll('pre').forEach((pre) => {
    if (pre.querySelector('.copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.textContent = '复制';
    btn.onclick = () => {
      const code = pre.querySelector('code');
      navigator.clipboard.writeText(code ? code.innerText : pre.innerText)
        .then(() => { btn.textContent = '已复制'; setTimeout(() => (btn.textContent = '复制'), 1400); })
        .catch(() => toast('复制失败，请手动选择', 'err'));
    };
    pre.appendChild(btn);
  });
  root.querySelectorAll('a[href^="http"]').forEach((a) => {
    a.target = '_blank'; a.rel = 'noopener noreferrer';
  });
}

/* ==================== 会话 ==================== */
async function loadSessions() {
  const res = await api.get('/api/sessions');
  state.sessions = res.sessions || [];
  renderSessions();
}

function renderSessions(filter = '') {
  const box = $('sessionList');
  const kw = filter.trim().toLowerCase();
  const list = state.sessions.filter((s) => !kw || (s.title || '').toLowerCase().includes(kw));
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="muted small" style="padding:12px 10px">暂无会话</div>';
    return;
  }
  list.forEach((s) => {
    const el = document.createElement('div');
    el.className = 'session-item' + (s.id === state.sessionId ? ' active' : '');
    el.innerHTML = `<span class="s-title">${esc(s.title || '新对话')}</span>
                    <span class="s-meta">${fmtTime(s.updated_at)}</span>
                    <span class="s-del" title="删除">🗑</span>`;
    el.onclick = (ev) => {
      if (ev.target.classList.contains('s-del')) {
        ev.stopPropagation();
        if (confirm(`删除会话「${s.title}」？`)) {
          api.del('/api/sessions/' + s.id).then(() => {
            if (state.sessionId === s.id) newChat(false);
            loadSessions();
            toast('会话已删除', 'ok');
          });
        }
        return;
      }
      openSession(s.id);
    };
    box.appendChild(el);
  });
}

async function openSession(sid) {
  state.sessionId = sid;
  const res = await api.get(`/api/sessions/${sid}/messages`);
  state.messages = res.messages || [];
  const meta = state.sessions.find((x) => x.id === sid);
  $('chatTitle').textContent = meta ? meta.title : '对话';
  renderMessages();
  renderSessions($('sessionSearch').value);
  $('sidebar').classList.remove('mobile-open');
  document.querySelector('.app').classList.remove('mobile-open');
}

function newChat(focus = true) {
  state.sessionId = null;
  state.messages = [];
  state.abort && state.abort.abort();
  $('chatTitle').textContent = '新对话';
  $('chatSub').textContent = '先查本地知识库 · 未命中再联网';
  $('routePill').hidden = true;
  renderMessages();
  renderSessions($('sessionSearch').value);
  if (focus) $('input').focus();
}

/* ==================== 消息渲染 ==================== */
function renderMessages() {
  const box = $('messages');
  box.innerHTML = '';
  if (!state.messages.length) {
    box.appendChild(buildWelcome());
    return;
  }
  state.messages.forEach((m) => box.appendChild(buildMessage(m)));
  scrollBottom(true);
}

function buildWelcome() {
  const wrap = document.createElement('div');
  wrap.className = 'welcome';
  wrap.innerHTML = `
    <div class="welcome-logo">🛠️</div>
    <h2>本地运维知识助手</h2>
    <p class="welcome-sub">优先检索你的私有知识库（Linux / K8s / 数据库 / 网络 / 可观测性 / 安全 / AI 基础设施 …），
      未覆盖时自动联网检索，并把结果沉淀到待审核区。</p>
    <div class="chips">${QUICK_CHIPS.map((c) => `<button class="chip">${esc(c)}</button>`).join('')}</div>
    <div class="welcome-hint"><span>💡 知识库靠关键词与别名匹配，问具体现象比问抽象概念更容易命中</span></div>`;
  wrap.querySelectorAll('.chip').forEach((b) => {
    b.onclick = () => { $('input').value = b.textContent; autoGrow(); send(); };
  });
  return wrap;
}

function buildMessage(m) {
  const isUser = m.role === 'user';
  const el = document.createElement('div');
  el.className = 'msg ' + (isUser ? 'user' : 'assistant');
  el.dataset.id = m.id || '';

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = isUser ? '👤' : '🛠️';

  const content = document.createElement('div');
  content.className = 'content';

  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  if (isUser) {
    bubble.textContent = m.content;
  } else {
    const md = document.createElement('div');
    md.className = 'markdown';
    md.innerHTML = renderMarkdown(m.content);
    decorate(md);
    bubble.appendChild(md);
  }
  content.appendChild(bubble);

  // 来源卡片 + 工具条
  const meta = m.meta || {};
  const kbSrc = meta.sources || [];
  const webSrc = meta.web_sources || [];
  if (!isUser && (kbSrc.length || webSrc.length)) {
    content.appendChild(buildSources(kbSrc, webSrc));
  }
  if (!isUser && m.id) {
    const tools = document.createElement('div');
    tools.className = 'msg-tools';
    const copy = document.createElement('button');
    copy.textContent = '复制全文';
    copy.onclick = () => navigator.clipboard.writeText(m.content).then(() => toast('已复制', 'ok'));
    tools.appendChild(copy);
    if (meta.usage && meta.usage.total_tokens) {
      const info = document.createElement('span');
      info.className = 'muted small';
      info.style.alignSelf = 'center';
      info.textContent = `${meta.usage.total_tokens} tokens · ${meta.elapsed || 0}s`;
      tools.appendChild(info);
    }
    content.appendChild(tools);
  }

  el.appendChild(avatar);
  el.appendChild(content);
  return el;
}

function buildSources(kbSrc, webSrc) {
  const wrap = document.createElement('div');
  wrap.className = 'sources';
  kbSrc.forEach((s) => {
    const card = document.createElement('div');
    card.className = 'source-card';
    card.innerHTML = `<div class="sc-top"><span class="sc-id">${esc(s.doc_id)}</span>
        <span>${esc(s.title || s.rel)}</span></div>
      <div class="sc-body">${esc(s.heading ? s.heading + ' · ' : '')}${esc(s.body || '')}</div>`;
    card.onclick = () => openDoc(s.rel);
    wrap.appendChild(card);
  });
  webSrc.forEach((s, i) => {
    const card = document.createElement('div');
    card.className = 'source-card';
    card.innerHTML = `<div class="sc-top"><span class="sc-id web">${i + 1}</span>
        <span>${esc(s.title || s.url)}</span></div>
      <div class="sc-body">${esc(s.url)}</div>`;
    card.onclick = () => window.open(s.url, '_blank', 'noopener');
    wrap.appendChild(card);
  });
  return wrap;
}

function scrollBottom(force) {
  const box = $('messages');
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 220;
  if (force || near) box.scrollTop = box.scrollHeight;
}

/* ==================== 发送 ==================== */
function stageEl(text) {
  const el = document.createElement('div');
  el.className = 'stage';
  el.innerHTML = `<span class="spin"></span><span>${esc(text)}</span>`;
  return el;
}

async function send() {
  if (state.streaming) return;
  const text = $('input').value.trim();
  if (!text) return;

  $('input').value = '';
  autoGrow();
  const welcome = $('messages').querySelector('.welcome');
  if (welcome) welcome.remove();

  // 用户消息
  const userMsg = { role: 'user', content: text, id: 0 };
  state.messages.push(userMsg);
  $('messages').appendChild(buildMessage(userMsg));
  scrollBottom(true);

  // 助手占位
  const holder = document.createElement('div');
  holder.className = 'msg assistant';
  holder.innerHTML = '<div class="avatar">🛠️</div><div class="content"></div>';
  const cbox = holder.querySelector('.content');
  const st = stageEl('正在检索本地知识库…');
  cbox.appendChild(st);
  $('messages').appendChild(holder);
  scrollBottom(true);

  state.streaming = true;
  $('btnSend').classList.add('stop');
  $('stageText').textContent = '';
  $('usageText').textContent = '';
  state.abort = new AbortController();

  let raw = '';
  let bubble = null, mdBox = null;
  let meta = { hits: [], web: [], route: 'none', confidence: 0 };
  let pendingRender = false;

  const ensureBubble = () => {
    if (bubble) return;
    st.remove();
    bubble = document.createElement('div');
    bubble.className = 'bubble streaming';
    mdBox = document.createElement('div');
    mdBox.className = 'markdown';
    bubble.appendChild(mdBox);
    cbox.appendChild(bubble);
  };
  const scheduleRender = () => {
    if (pendingRender) return;
    pendingRender = true;
    requestAnimationFrame(() => {
      pendingRender = false;
      if (mdBox) { mdBox.innerHTML = renderMarkdown(raw); scrollBottom(false); }
    });
  };

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: state.sessionId, message: text, mode: state.mode }),
      signal: state.abort.signal
    });
    if (!resp.ok || !resp.body) throw new Error('HTTP ' + resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = { event: 'message', data: '' };
        block.split('\n').forEach((line) => {
          if (line.startsWith('event:')) ev.event = line.slice(6).trim();
          else if (line.startsWith('data:')) ev.data += line.slice(5).trim();
        });
        if (!ev.data) continue;
        let payload = {};
        try { payload = JSON.parse(ev.data); } catch (e) { payload = { t: ev.data }; }

        handleEvent(ev.event, payload);
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      toast('已停止生成', 'info');
    } else {
      ensureBubble();
      raw += `\n\n> ⚠️ 请求失败：${esc(e.message)}`;
      scheduleRender();
      toast('请求失败：' + esc(e.message), 'err');
    }
  } finally {
    state.streaming = false;
    state.abort = null;
    $('btnSend').classList.remove('stop');
    $('stageText').textContent = '';
    if (bubble) bubble.classList.remove('streaming');
    if (mdBox) { mdBox.innerHTML = renderMarkdown(raw); decorate(mdBox); }
    // 落本地状态
    if (raw) {
      state.messages.push({
        role: 'assistant', content: raw, id: Date.now(),
        meta: { sources: meta.hits, web_sources: meta.web, route: meta.route, confidence: meta.confidence }
      });
      if (meta.hits.length || meta.web.length) {
        cbox.appendChild(buildSources(meta.hits, meta.web));
      }
      // 知识库没答全时，给一个"联网重答"的出口（比让用户手动改模式更顺手）
      const hedged = /未覆盖|知识库中未|知识库里没有|不足以回答|未提及|没有找到/.test(raw);
      if (meta.route === 'kb' && hedged) {
        const bar = document.createElement('div');
        bar.className = 'retry-bar';
        const btn = document.createElement('button');
        btn.className = 'retry-btn';
        btn.textContent = '🌐 知识库没答全？用联网检索重答';
        btn.onclick = () => {
          setMode('web');
          $('input').value = text;
          autoGrow();
          send();
        };
        bar.appendChild(btn);
        cbox.appendChild(bar);
      }
    }
    loadSessions();
    refreshDraftCount();
    scrollBottom(true);
  }

  function handleEvent(event, p) {
    if (event === 'stage') {
      $('stageText').textContent = p.text || '';
      const s = cbox.querySelector('.stage span:last-child');
      if (s) s.textContent = p.text || '';
      return;
    }
    if (event === 'session') { state.sessionId = p.id; return; }
    if (event === 'meta') {
      meta.hits = p.hits || [];
      const route = p.route;
      meta.route = route;
      meta.confidence = p.confidence || 0;
      const pill = $('routePill');
      pill.hidden = false;
      const labels = {
        kb: `📚 知识库命中 ${(p.confidence * 100).toFixed(0)}%`,
        web: '🌐 联网检索',
        both: '📚+🌐 混合',
        none: '⚠️ 未命中'
      };
      pill.className = 'route-pill ' + route;
      pill.textContent = labels[route] || route;
      $('chatSub').textContent = route === 'kb'
        ? `知识库命中（置信度 ${(p.confidence * 100).toFixed(0)}%）`
        : (route === 'none' ? '知识库与联网均未取得有效内容' : '知识库未覆盖 → 已联网检索');
      return;
    }
    if (event === 'web') { meta.web = (p.pages || []).map((x) => ({ title: x.title, url: x.url, engine: x.engine })); return; }
    if (event === 'delta') {
      ensureBubble();
      raw += p.t || '';
      scheduleRender();
      return;
    }
    if (event === 'sources') {
      meta.hits = p.kb || [];
      if (p.web && p.web.length) meta.web = p.web;
      meta.route = p.route || meta.route;
      meta.confidence = p.confidence || meta.confidence;
      return;
    }
    if (event === 'error') {
      ensureBubble();
      raw += `\n\n> ⚠️ ${esc(p.message || '未知错误')}`;
      scheduleRender();
      toast(esc(p.message || '未知错误'), 'err', 5000);
      return;
    }
    if (event === 'done') {
      if (p.usage && p.usage.total_tokens) {
        $('usageText').textContent = `${p.usage.total_tokens} tokens · ${p.elapsed}s`;
      }
      if (p.draft && p.draft.saved) {
        toast(`已写入待审核区：<b>${esc(p.draft.title || p.draft.name)}</b><br><span class="muted small">核对后可一键采纳入库</span>`, 'ok', 6000);
        refreshDraftCount();
      }
      return;
    }
  }
}

/* ==================== 设置 ==================== */
async function openSettings() {
  const res = await api.get('/api/settings');
  const s = res.settings || {};
  state.settings = s;
  $('sModel').value = s.model || '';
  $('sBaseUrl').value = s.base_url || '';
  $('sApiKey').value = '';
  $('sKeyHint').textContent = `当前 Key：${s.api_key_masked || '未配置'}（留空则不修改）`;
  $('sTemp').value = s.temperature;
  $('sTempVal').textContent = s.temperature;
  $('sMaxTokens').value = s.max_tokens;
  $('sTopK').value = s.top_k;
  $('sTh').value = s.kb_score_threshold;
  $('sThVal').textContent = s.kb_score_threshold;
  $('sHistory').value = s.history_turns;
  $('sMaxCtx').value = s.max_context_chars;
  $('sKbDir').value = s.kb_dir || '';
  $('sWebEnabled').checked = !!s.web_search_enabled;
  $('sEngines').value = (s.web_engines || []).join(',');
  $('sWebPages').value = s.web_max_pages;
  $('sWebChars').value = 6000;
  $('sWebTimeout').value = s.web_timeout;
  $('sIngest').value = s.auto_ingest || 'inbox';
  show('modalSettings');
}

async function saveSettings() {
  const updates = {
    MODEL: $('sModel').value.trim(),
    DEEPSEEK_BASE_URL: $('sBaseUrl').value.trim(),
    TEMPERATURE: $('sTemp').value,
    MAX_TOKENS: $('sMaxTokens').value,
    TOP_K: $('sTopK').value,
    KB_SCORE_THRESHOLD: $('sTh').value,
    HISTORY_TURNS: $('sHistory').value,
    MAX_CONTEXT_CHARS: $('sMaxCtx').value,
    KB_DIR: $('sKbDir').value.trim(),
    WEB_SEARCH_ENABLED: $('sWebEnabled').checked ? 'true' : 'false',
    WEB_ENGINES: $('sEngines').value.trim(),
    WEB_MAX_PAGES: $('sWebPages').value,
    WEB_MAX_CHARS: $('sWebChars').value,
    WEB_TIMEOUT: $('sWebTimeout').value,
    AUTO_INGEST: $('sIngest').value
  };
  const body = { updates };
  const key = $('sApiKey').value.trim();
  if (key) body.api_key = key;
  const res = await api.post('/api/settings', body);
  if (res.ok) {
    toast('设置已保存', 'ok');
    hide('modalSettings');
    refreshHealth();
  } else {
    toast('保存失败：' + esc(res.error || ''), 'err');
  }
}

/* ==================== 知识库浏览 ==================== */
let kbDocsCache = [];
async function openKb() {
  const res = await api.get('/api/kb/docs');
  kbDocsCache = res.docs || [];
  renderKbList('');
  show('modalKb');
}

function renderKbList(kw) {
  const box = $('kbList');
  const k = kw.trim().toLowerCase();
  const list = kbDocsCache.filter((d) =>
    !k || [d.doc_id, d.title, d.volume, d.level].some((v) => String(v || '').toLowerCase().includes(k)));
  box.innerHTML = list.length ? '' : '<div class="muted small">没有匹配的文档</div>';
  list.slice(0, 300).forEach((d) => {
    const el = document.createElement('div');
    el.className = 'kb-item';
    el.innerHTML = `<div class="k-top"><span class="tag-id">${esc(d.doc_id)}</span>
        <span>${esc(d.title)}</span></div>
      <div class="k-sub"><span class="tag-vol">${esc(d.volume)}</span> · ${esc(d.level)} · ${esc(d.rel)}</div>`;
    el.onclick = () => { hide('modalKb'); openDoc(d.rel); };
    box.appendChild(el);
  });
}

async function openDoc(rel) {
  if (!rel) return;
  const res = await api.get('/api/kb/doc?rel=' + encodeURIComponent(rel));
  if (!res.ok) return toast('无法读取该文档', 'err');
  const d = res.doc;
  $('docTitle').textContent = d.meta.title || rel;
  $('docMeta').innerHTML = Object.entries(d.meta)
    .map(([k, v]) => `<span class="tag-vol">${esc(k)}: ${esc(String(v).slice(0, 80))}</span>`).join('');
  const body = d.text.replace(/^---[\s\S]*?\n---\n/, '');
  const box = $('docBody');
  box.innerHTML = renderMarkdown(body);
  decorate(box);
  show('modalDoc');
}

/* ==================== 草稿 ==================== */
async function refreshDraftCount() {
  const res = await api.get('/api/drafts');
  state.drafts = res.drafts || [];
  $('draftCount').textContent = state.drafts.length;
}

async function openDrafts() {
  await refreshDraftCount();
  const box = $('draftList');
  box.innerHTML = state.drafts.length ? '' : '<div class="muted small">暂无待审核草稿</div>';
  state.drafts.forEach((d) => {
    const el = document.createElement('div');
    el.className = 'draft-item';
    el.innerHTML = `<div class="k-top">📄 <span>${esc(d.title)}</span></div>
      <div class="k-sub">${esc(d.status)} · ${esc(d.level)} · ${(d.size / 1024).toFixed(1)} KB · ${esc(d.updated)}</div>`;
    el.onclick = () => openReview(d.name);
    box.appendChild(el);
  });
  show('modalDrafts');
}

let reviewName = null;
async function openReview(name) {
  const res = await api.get('/api/drafts/read?name=' + encodeURIComponent(name));
  if (!res.ok) return toast('草稿不存在', 'err');
  const d = res.draft;
  reviewName = name;
  $('reviewTitle').textContent = '草稿审核 · ' + name;
  $('rTitle').value = d.meta.title || '';
  $('rTags').value = d.meta.tags || '';
  $('rLevel').value = d.meta.level || 'L2';
  $('rPreview').textContent = d.raw.slice(0, 6000);

  const volRes = await api.get('/api/kb/volumes');
  const sel = $('rVolume');
  sel.innerHTML = (volRes.volumes || []).map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join('');
  const guessed = guessVolume(d.meta.title || '', d.meta.tags || '');
  if (guessed) sel.value = guessed;
  await autoId();

  hide('modalDrafts');
  show('modalReview');
}

function guessVolume(title, tags) {
  const t = (title + ' ' + tags).toLowerCase();
  const rules = [
    [['kubernetes', 'k8s', 'pod', '容器', 'docker', 'istio', 'helm'], 'H-云原生'],
    [['mysql', 'redis', 'kafka', '数据库', 'elasticsearch', 'mongodb', '存储'], 'F-数据存储与中间件'],
    [['linux', '内核', 'systemd', '进程', 'cgroup'], 'D-操作系统与系统编程'],
    [['网络', 'tcp', 'dns', 'bgp', '负载均衡', 'nginx'], 'E-计算机网络'],
    [['prometheus', '监控', '日志', '可观测', '告警', 'zabbix'], 'J-可观测性'],
    [['安全', '漏洞', '攻击', '加密', '合规', '密码'], 'K-安全'],
    [['ci/cd', 'cicd', 'gitops', 'terraform', 'ansible', '流水线'], 'I-工程化与DevOps'],
    [['分布式', '一致性', '微服务', '架构'], 'G-分布式系统与架构'],
    [['云平台', 'vpc', '阿里云', 'aws', '虚拟化', 'kvm'], 'L-云平台与数据中心'],
    [['机器学习', '深度学习', '大模型', 'gpu', 'ai'], 'M-人工智能与数据科学']
  ];
  for (const [keys, vol] of rules) {
    if (keys.some((k) => t.includes(k))) return vol;
  }
  return '';
}

async function autoId() {
  const vol = $('rVolume').value;
  const res = await api.get('/api/kb/search?q=' + encodeURIComponent(vol) + '&top_k=1');
  // 用前端缓存推算：取该卷已有最大编号 +1
  const letter = (vol.match(/^[A-Za-z0-9]+/) || ['X'])[0][0].toUpperCase();
  const nums = kbDocsCache
    .filter((d) => String(d.doc_id || '').toUpperCase().startsWith(letter))
    .map((d) => parseInt(String(d.doc_id).slice(1), 10))
    .filter((n) => !isNaN(n));
  const next = (nums.length ? Math.max(...nums) : 0) + 1;
  $('rId').value = letter + String(next).padStart(2, '0');
}

async function promoteDraft() {
  if (!reviewName) return;
  const res = await api.post('/api/drafts/promote', {
    name: reviewName,
    volume: $('rVolume').value,
    doc_id: $('rId').value.trim(),
    title: $('rTitle').value.trim(),
    tags: $('rTags').value.trim(),
    level: $('rLevel').value,
    register_index: $('rRegister').checked
  });
  if (!res.ok) return toast('采纳失败：' + esc(res.error || ''), 'err', 5000);
  const idx = res.index_registered && res.index_registered.ok;
  toast(`已入库：<b>${esc(res.doc_id)}</b> → ${esc(res.dest.split('\\').pop())}<br>
         <span class="muted small">索引已重建${idx ? ' · 总索引已登记' : '（总索引未登记：' + esc((res.index_registered || {}).reason || '') + '）'}</span>`,
        'ok', 6000);
  hide('modalReview');
  refreshDraftCount();
  refreshHealth();
}

/* ==================== 健康状态 ==================== */
async function refreshHealth() {
  try {
    const h = await api.get('/api/health');
    const kb = h.kb || {};
    const dot = $('kbDot');
    if (!kb.kb_dir_exists) {
      dot.className = 'dot err';
      $('kbStatusText').textContent = '知识库目录不存在';
    } else if (kb.stale) {
      dot.className = 'dot';
      $('kbStatusText').textContent = '索引待更新（点 ♻️ 重建）';
    } else {
      dot.className = 'dot ok';
      $('kbStatusText').textContent = `${kb.docs} 篇 / ${kb.chunks} 块`;
    }
    $('brandSub').textContent = `${kb.docs || 0} 篇知识库 · ${h.llm.api_key_set ? 'API 就绪' : 'API 未配置'}`;
    return h;
  } catch (e) {
    $('kbDot').className = 'dot err';
    $('kbStatusText').textContent = '服务未连接';
    return null;
  }
}

async function reindex() {
  toast('正在重建索引…', 'info');
  const res = await api.post('/api/kb/reindex', {});
  if (res.ok) {
    const r = res.result || {};
    toast(`索引已重建：${r.docs} 篇 / ${r.chunks} 块（${r.seconds}s）`, 'ok');
    refreshHealth();
  } else {
    toast('重建失败', 'err');
  }
}

/* ==================== 弹窗与杂项 ==================== */
function show(id) { $(id).hidden = false; }
function hide(id) { $(id).hidden = true; }

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.mode-btn').forEach((x) => x.classList.toggle('active', x.dataset.mode === mode));
}

function autoGrow() {
  const el = $('input');
  if (!el.value) { el.style.height = ''; return; }   // 空输入框保持单行高
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 190) + 'px';
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem('ops_theme', theme);
  $('hljs-dark').disabled = theme !== 'dark';
  $('hljs-light').disabled = theme === 'dark';
}

/* ==================== 事件绑定 ==================== */
function bind() {
  $('btnNewChat').onclick = () => newChat();
  $('btnCollapse').onclick = () => document.querySelector('.app').classList.toggle('collapsed');
  $('btnMenu').onclick = () => document.querySelector('.app').classList.toggle('mobile-open');
  $('btnTheme').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  $('btnReindex').onclick = reindex;
  $('btnSettings').onclick = openSettings;
  $('btnKb').onclick = openKb;
  $('btnDrafts').onclick = openDrafts;
  $('btnSend').onclick = () => { if (state.streaming) { state.abort && state.abort.abort(); } else { send(); } };
  $('btnSaveSettings').onclick = saveSettings;
  $('btnAutoId').onclick = autoId;
  $('btnPromote').onclick = promoteDraft;
  $('btnTestLlm').onclick = async () => {
    toast('正在测试…', 'info', 1500);
    const res = await api.get('/api/llm/health');
    const r = (res.result || {});
    r.ok ? toast(`连接正常 · ${esc(r.model)}`, 'ok') : toast('连接失败：' + esc(r.error || ''), 'err', 6000);
  };
  $('btnEnrich').onclick = async () => {
    if (!reviewName) return;
    toast('AI 正在提炼元数据…', 'info', 2500);
    const res = await api.post('/api/drafts/enrich', { name: reviewName });
    if (!res.ok) return toast('提炼失败：' + esc(res.error || ''), 'err');
    const m = res.meta || {};
    $('rTitle').value = m.title || $('rTitle').value;
    $('rTags').value = (m.tags || []).join(', ');
    $('rLevel').value = m.level || 'L2';
    toast('元数据已更新（标题/标签/级别）', 'ok');
  };
  $('btnDeleteDraft').onclick = async () => {
    if (!reviewName || !confirm('确定删除该草稿？')) return;
    await api.post('/api/drafts/delete', { name: reviewName });
    toast('草稿已删除', 'ok');
    hide('modalReview');
    refreshDraftCount();
  };
  $('btnStop').onclick = async () => {
    if (!confirm('确定停止本地服务？（下次访问需重新启动）')) return;
    await api.post('/api/shutdown', {});
    document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font-family:sans-serif;color:#9aa9bd">服务已停止 · 可关闭此页面</div>';
  };
  $('sessionSearch').oninput = (e) => renderSessions(e.target.value);
  $('kbSearch').oninput = (e) => renderKbList(e.target.value);
  $('input').addEventListener('input', autoGrow);
  $('input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  document.querySelectorAll('.mode-btn').forEach((b) => {
    b.onclick = () => {
      document.querySelectorAll('.mode-btn').forEach((x) => x.classList.remove('active'));
      b.classList.add('active');
      state.mode = b.dataset.mode;
    };
  });
  $('sTemp').oninput = (e) => ($('sTempVal').textContent = e.target.value);
  $('sTh').oninput = (e) => ($('sThVal').textContent = e.target.value);
  $('rVolume').onchange = autoId;
  document.querySelectorAll('[data-close]').forEach((b) => {
    b.onclick = () => b.closest('.modal').hidden = true;
  });
  document.querySelectorAll('.modal').forEach((m) => {
    m.onclick = (e) => { if (e.target === m) m.hidden = true; };
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') document.querySelectorAll('.modal').forEach((m) => (m.hidden = true));
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') { e.preventDefault(); newChat(); }
  });
}

/* ==================== 启动 ==================== */
async function boot() {
  applyTheme(localStorage.getItem('ops_theme') || 'dark');
  bind();
  renderMessages();          // 首屏渲染欢迎区（含快捷提问 chips）
  await refreshHealth();
  await loadSessions();
  await refreshDraftCount();
  autoGrow();
  $('input').focus();
  setInterval(refreshHealth, 60000);
}
boot();
